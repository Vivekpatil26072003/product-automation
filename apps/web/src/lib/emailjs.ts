// EmailJS channel (EMAIL_PROVIDER=emailjs on the API). The browser sends through EmailJS with the PUBLIC key; the
// API records the confirmed intent, hands out the template variables once (built from the latest saved report
// and draft) and records what EmailJS answered. No private key is used or needed in the browser.

import { EmailJSResponseStatus, send } from "@emailjs/browser";

export type EmailJsConfig = { serviceId: string; templateId: string; publicKey: string };

export function emailJsConfig(): EmailJsConfig | null {
  const serviceId = process.env.NEXT_PUBLIC_EMAILJS_SERVICE_ID ?? "";
  const templateId = process.env.NEXT_PUBLIC_EMAILJS_TEMPLATE_ID ?? "";
  const publicKey = process.env.NEXT_PUBLIC_EMAILJS_PUBLIC_KEY ?? "";
  return serviceId && templateId && publicKey ? { serviceId, templateId, publicKey } : null;
}

export function missingEmailJsConfig(): string[] {
  return [
    ["NEXT_PUBLIC_EMAILJS_SERVICE_ID", process.env.NEXT_PUBLIC_EMAILJS_SERVICE_ID],
    ["NEXT_PUBLIC_EMAILJS_TEMPLATE_ID", process.env.NEXT_PUBLIC_EMAILJS_TEMPLATE_ID],
    ["NEXT_PUBLIC_EMAILJS_PUBLIC_KEY", process.env.NEXT_PUBLIC_EMAILJS_PUBLIC_KEY],
  ].filter(([, v]) => !v).map(([k]) => k as string);
}

export type SendResult = { outcome: "ACCEPTED" | "FAILED" | "UNKNOWN"; status: number | null; text: string };

/** Template variables must all be strings: anything missing becomes "" (never undefined or null). */
export function cleanParams(params: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.entries(params).map(([k, v]) => [k, v === null || v === undefined ? "" : String(v)]));
}

/** 200 = accepted by EmailJS; 4xx = refused (fix and resend explicitly); no answer = unknown (reconcile). */
export function classify(error: unknown): SendResult {
  if (error instanceof EmailJSResponseStatus && error.status > 0) {
    return { outcome: error.status >= 500 ? "UNKNOWN" : "FAILED", status: error.status, text: error.text || "EmailJS refused the request." };
  }
  return { outcome: "UNKNOWN", status: null, text: error instanceof Error ? error.message : "No answer from EmailJS." };
}

export async function sendWithEmailJs(config: EmailJsConfig, params: Record<string, unknown>): Promise<SendResult> {
  try {
    const res = await send(config.serviceId, config.templateId, cleanParams(params), { publicKey: config.publicKey });
    return { outcome: "ACCEPTED", status: res.status, text: res.text };
  } catch (error) {
    return classify(error);
  }
}
