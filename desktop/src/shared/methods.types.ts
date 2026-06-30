export type ControlType =
  | 'text' | 'textarea' | 'integer' | 'number' | 'slider'
  | 'toggle' | 'select' | 'combobox' | 'tags' | 'json' | 'file'

export type RefKind =
  | 'source' | 'entity' | 'claim' | 'page' | 'relation'
  | 'node' | 'proposal' | 'session'

export type FileMode = 'open' | 'save' | 'under-root'

export interface Param {
  name: string
  type: string
  required?: boolean
  default?: string
  description?: string
  control: ControlType
  enum?: string[]
  combobox?: boolean
  refKind?: RefKind
  refMulti?: boolean
  file?: FileMode
}

export interface Method {
  name: string
  group?: string
  summary?: string
  returns?: string
  params?: Param[]
  view: string
  longRunning?: boolean
  gated?: boolean
  mutates?: boolean
}
