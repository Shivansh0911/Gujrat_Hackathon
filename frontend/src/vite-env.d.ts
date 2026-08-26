/// <reference types="vite/client" />

/**
 * Build-time configuration Vite inlines into the bundle.
 *
 * Declared explicitly rather than relying on the ambient reference alone, so a
 * missing or misspelled variable is a compile error rather than `undefined` at
 * runtime in a deployed container.
 */
interface ImportMetaEnv {
  /** API origin. Defaults to same-origin `/api`, proxied by nginx or the dev server. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
