// Configuration injected at build time via environment variables.
// Vite exposes env vars prefixed with VITE_ on import.meta.env.

export const config = {
  apiBase: import.meta.env.VITE_API_BASE || "https://pho8lsqt7d.execute-api.us-west-2.amazonaws.com/dev",
  cognitoUserPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID || "us-west-2_zJKQvCJyU",
  cognitoClientId: import.meta.env.VITE_COGNITO_CLIENT_ID || "11nf0ae6j041drg51auofqgi8r",
  cognitoRegion: import.meta.env.VITE_COGNITO_REGION || "us-west-2",
};
