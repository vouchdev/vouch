from vouch import health, index_db
from vouch.models import Claim
from vouch.storage import KBStore


def test_rebuild_index_preserves_embedding_index(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    store.put_claim(Claim(id="c1", text="semantic target phrase", evidence=[src.id]))
    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None

    health.rebuild_index(store)

    assert index_db.get_embedding(store.kb_dir, kind="claim", id="c1") is not None
    hits = index_db.search_semantic(store.kb_dir, "semantic target phrase", limit=5)
    assert hits
    assert hits[0][1] == "c1"
