/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
// Object storage origin: signed upload (PUT), PDF preview (frame) and page images come from there.
const storage = process.env.STORAGE_PUBLIC_ORIGIN ?? "http://localhost:9000";
const production = process.env.NODE_ENV === "production";
// Production-only CSP (FR25). Development needs eval for fast refresh, so it is not applied there.
const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob: ${storage}`,
  // api.emailjs.com: report emails are sent from the browser when EMAIL_PROVIDER=emailjs.
  `connect-src 'self' ${storage} https://api.emailjs.com`,
  `frame-src ${storage}`,
  "media-src 'self' blob:",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Same-origin API: the browser only talks to this app; cookies and CSRF stay first-party.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "Permissions-Policy", value: "camera=(self), microphone=(), geolocation=()" },
          ...(production ? [
            { key: "Content-Security-Policy", value: csp },
            { key: "X-Frame-Options", value: "DENY" },
            { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
          ] : []),
        ],
      },
    ];
  },
};

export default nextConfig;
