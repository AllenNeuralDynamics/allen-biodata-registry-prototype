// Minimal Cognito authentication via the Cognito hosted UI.
//
// We redirect to the User Pool's hosted UI for sign-in (avoids bundling
// SRP libraries in the SPA). After login Cognito redirects back with the
// id_token in the URL fragment; we parse and stash it in localStorage.

import { config } from "./config";

const TOKEN_KEY = "biodata_registry_id_token";
const TOKEN_EXP_KEY = "biodata_registry_id_token_exp";

// The hosted UI domain is provisioned by the cognito Terraform module
// as `<name_prefix>-auth-<account_id>.auth.<region>.amazoncognito.com`.
// We let it be overridden via VITE_COGNITO_DOMAIN so dev environments
// can point to a different prefix without rebuilding.
const HOSTED_UI_DOMAIN =
  (import.meta as any).env?.VITE_COGNITO_DOMAIN ||
  "https://biodata-registry-dev-auth-014097726564.auth.us-west-2.amazoncognito.com";

export function getRedirectUri(): string {
  return `${window.location.origin}/`;
}

export function login(): void {
  const redirect = encodeURIComponent(getRedirectUri());
  // Hosted UI authorization-code grant.
  const url = `${HOSTED_UI_DOMAIN}/login?client_id=${config.cognitoClientId}&response_type=token&scope=email+openid+profile&redirect_uri=${redirect}`;
  window.location.assign(url);
}

export function logout(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_EXP_KEY);
  const redirect = encodeURIComponent(getRedirectUri());
  const url = `${HOSTED_UI_DOMAIN}/logout?client_id=${config.cognitoClientId}&logout_uri=${redirect}`;
  window.location.assign(url);
}

export function getToken(): string | null {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return null;
  const exp = parseInt(localStorage.getItem(TOKEN_EXP_KEY) || "0", 10);
  if (exp && Date.now() > exp * 1000) {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_EXP_KEY);
    return null;
  }
  return token;
}

export function captureTokenFromHash(): boolean {
  if (!window.location.hash) return false;
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const token = params.get("id_token");
  const expIn = parseInt(params.get("expires_in") || "3600", 10);
  if (!token) return false;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(TOKEN_EXP_KEY, String(Math.floor(Date.now() / 1000) + expIn));
  history.replaceState(null, "", window.location.pathname);
  return true;
}

export function decodeJwt(token: string): { email?: string; sub?: string; exp?: number } | null {
  try {
    const payload = token.split(".")[1];
    const decoded = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(decoded);
  } catch {
    return null;
  }
}
