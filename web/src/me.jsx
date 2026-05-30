/**
 * v1.7.1 user-mode / dev-mode split. v1.14.1 adds dev "view as user" toggle.
 *
 * `MeProvider` fetches /api/me once on mount and exposes the current user +
 * statics + is_developer flag to the whole tree. Components call `useMe()`
 * to read it; the static-switcher writes via `refresh()` after mutations.
 *
 * **View-as-user (v1.14.1)**: devs can flip a toggle to render the
 * consumer Home / hide dev-only surfaces — useful for testing the
 * non-dev experience without DB poking. The override is applied
 * inside the context so existing consumers (which read `is_developer`)
 * transparently see the masked value. `actual_is_developer` is exposed
 * separately so the toggle button itself knows the real status.
 * Persisted to localStorage so the toggle survives reloads.
 *
 * Why central: every page wants is_developer to gate dev-only surfaces
 * (Abilities tab, FieldStats panel, "show all" toggles). Without a shared
 * fetch we'd hit /api/me three times on every navigation.
 */
import { createContext, useCallback, useContext, useEffect, useState } from 'react';

const MeContext = createContext(null);
const VIEW_AS_USER_KEY = 'vigil.viewAsUser';

export function MeProvider({ children }) {
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);
  const [viewAsUser, setViewAsUserState] = useState(() => {
    try {
      return localStorage.getItem(VIEW_AS_USER_KEY) === '1';
    } catch {
      return false;
    }
  });

  const setViewAsUser = useCallback((on) => {
    setViewAsUserState(on);
    try {
      if (on) localStorage.setItem(VIEW_AS_USER_KEY, '1');
      else localStorage.removeItem(VIEW_AS_USER_KEY);
    } catch { /* ignore quota / disabled */ }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch('/api/me');
      if (!r.ok) throw new Error(`GET /api/me ${r.status}`);
      setMe(await r.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // When viewAsUser is on AND the real user is a dev, render is_developer=false
  // to downstream consumers so they hide dev surfaces / show consumer Home.
  // Non-devs flipping the toggle would be a no-op; we still respect the
  // toggle so a dev who set it stays in user-view across reloads.
  const effectiveMe = me && me.is_developer && viewAsUser
    ? { ...me, is_developer: false }
    : me;

  return (
    <MeContext.Provider value={{
      me: effectiveMe,
      actual_is_developer: !!(me && me.is_developer),
      view_as_user: viewAsUser,
      setViewAsUser,
      error,
      refresh,
    }}>
      {children}
    </MeContext.Provider>
  );
}

export function useMe() {
  const ctx = useContext(MeContext);
  if (ctx === null) {
    throw new Error('useMe must be used inside <MeProvider>');
  }
  return ctx;
}
