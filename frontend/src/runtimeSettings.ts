export const RUNTIME_SETTINGS_CHANGED_KEY = "tilian:runtime-settings-changed";

export function notifyRuntimeSettingsChanged() {
  try {
    localStorage.setItem(RUNTIME_SETTINGS_CHANGED_KEY, crypto.randomUUID());
  } catch {
    // Periodic refresh still works when browser storage is unavailable.
  }
}
