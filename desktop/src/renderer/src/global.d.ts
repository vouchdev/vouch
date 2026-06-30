import type { VouchApi } from '../../shared/ipc'
declare global {
  interface Window { vouch: VouchApi }
}
export {}
