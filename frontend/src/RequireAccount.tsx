/* Gates a route behind sign-in.
 *
 * Login is the front door now: the home page's primary action is Sign In,
 * not straight into the console. This is the checkpoint every route past
 * that door goes through, so a bookmark to /projects or /scenes cannot skip
 * it either.
 */

import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, type Account } from "./api";

export function RequireAccount({ children }: { children: React.ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);

  useEffect(() => {
    api.me().then(setAccount).catch(() => setAccount({ signed_in: false }));
  }, []);

  if (account === null) return <div className="shell" />;
  if (!account.signed_in) return <Navigate to="/login" replace />;
  return <>{children}</>;
}
