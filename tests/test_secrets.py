"""Secret masking — keep credentials out of the capture buffer and durable
artifacts. High-precision curated patterns (not raw entropy), so ordinary
content like git shas and file paths is never mangled.
"""

from __future__ import annotations

from vouch.secrets import REDACTION, contains_secret, mask_secrets

# Assembled from fragments so no literal secret marker appears in this file
# (the repo's own secret-scan hook would flag it — which is the point).
_PK = "PRIV" + "ATE " + "KEY"


def test_masks_aws_access_key() -> None:
    out = mask_secrets("key is AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert REDACTION in out


def test_masks_github_token() -> None:
    tok = "ghp_" + "a" * 36
    assert tok not in mask_secrets(f"token={tok}")


def test_masks_openai_style_key() -> None:
    tok = "sk-" + "A1b2C3d4" * 4
    assert tok not in mask_secrets(f"export OPENAI_API_KEY={tok}")


def test_masks_bearer_token_but_keeps_the_word_bearer() -> None:
    out = mask_secrets("curl -H 'Authorization: Bearer abcDEF123456ghiJKL789'")
    assert "abcDEF123456ghiJKL789" not in out
    assert "Bearer" in out


def test_masks_key_value_assignment_but_keeps_the_key_name() -> None:
    out = mask_secrets("PASSWORD=hunter2supersecret")
    assert "hunter2supersecret" not in out
    assert "PASSWORD" in out


def test_masks_private_key_block() -> None:
    begin = f"-----BEGIN RSA {_PK}-----"
    end = f"-----END RSA {_PK}-----"
    block = f"{begin}\nMIIEpAIBAAKCAQEA7f8QZ\nabc123\n{end}"
    out = mask_secrets(f"here is a key:\n{block}\ndone")
    assert "MIIEpAIBAAKCAQEA7f8QZ" not in out
    assert "done" in out


def test_leaves_ordinary_content_untouched() -> None:
    # a git sha, a file path, a normal sentence — no false positives
    for text in (
        "Edited config.py at a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "Ran: pytest tests/ -q --limit=10",
        "the quick brown fox jumps over the lazy dog",
    ):
        assert mask_secrets(text) == text
        assert contains_secret(text) is False


def test_contains_secret_flags_a_secret() -> None:
    assert contains_secret("AKIAIOSFODNN7EXAMPLE") is True


# --- redact: remediation for a secret that reached a durable claim ---------


def test_redact_masks_claim_text_and_marks_redacted(tmp_path, monkeypatch) -> None:
    from vouch import audit
    from vouch import lifecycle as life
    from vouch.models import Claim, ClaimStatus
    from vouch.storage import KBStore

    store = KBStore.init(tmp_path)
    monkeypatch.chdir(store.root)
    src = store.put_source(b"e", title="d")
    store.put_claim(Claim(id="c1", text="the key is AKIAIOSFODNN7EXAMPLE", evidence=[src.id]))

    out = life.redact(store, claim_id="c1", actor="human")
    assert "AKIAIOSFODNN7EXAMPLE" not in out.text
    assert out.status is ClaimStatus.REDACTED

    reloaded = store.get_claim("c1")
    assert "AKIAIOSFODNN7EXAMPLE" not in reloaded.text
    assert reloaded.status is ClaimStatus.REDACTED
    assert any(e.event == "claim.redact" for e in audit.read_events(store.kb_dir))


def test_cli_redact_command(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli
    from vouch.models import Claim
    from vouch.storage import KBStore

    store = KBStore.init(tmp_path)
    monkeypatch.chdir(store.root)
    src = store.put_source(b"e", title="d")
    store.put_claim(Claim(id="c1", text="token=ghp_" + "a" * 36, evidence=[src.id]))

    result = CliRunner().invoke(cli, ["redact", "c1"])
    assert result.exit_code == 0, result.output
    assert "ghp_" not in store.get_claim("c1").text


def test_masks_json_quoted_key_credentials() -> None:
    """JSON / quoted-key forms must not leak past the assignment mask (#549)."""
    assert mask_secrets('"password": "hunter2secret"') == '"password": "[redacted-secret]"'
    assert mask_secrets("'api_key': 'abcdefghij'") == "'api_key': '[redacted-secret]'"
    assert mask_secrets('"token" : "abcdefghij"') == '"token" : "[redacted-secret]"'


def test_masks_quoted_value_with_whitespace_and_escapes() -> None:
    """Quoted values are whole units — whitespace must not leak a trailing token."""
    text = '"password": "hunter2 secret"'
    out = mask_secrets(text)
    assert "hunter2 secret" not in out
    assert "hunter2" not in out
    assert out == '"password": "[redacted-secret]"'

    # Value body includes escaped quotes: say \"hi\" nowxx
    escaped = '"password": "say \\"hi\\" nowxx"'
    assert '\\"hi\\"' in escaped
    out2 = mask_secrets(escaped)
    assert "nowxx" not in out2
    assert out2 == '"password": "[redacted-secret]"'


def test_masks_plain_assignment_still_works() -> None:
    assert "hunter2secret" not in mask_secrets("password=hunter2secret")
    assert "hunter2secret" not in mask_secrets("password: hunter2secret")
    assert mask_secrets("password=hunter2secret") == "password=[redacted-secret]"
