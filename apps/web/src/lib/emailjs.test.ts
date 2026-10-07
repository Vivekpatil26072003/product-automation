import { EmailJSResponseStatus } from "@emailjs/browser";
import { afterEach, describe, expect, it, vi } from "vitest";

import { classify, cleanParams, emailJsConfig, missingEmailJsConfig } from "./emailjs";

describe("emailjs", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("never passes null or undefined template variables", () => {
    expect(cleanParams({ to_email: "a@example.org", cc_email: null, bcc_email: undefined, record_count: 5 })).toEqual({
      to_email: "a@example.org", cc_email: "", bcc_email: "", record_count: "5",
    });
  });

  it("classifies EmailJS answers: 4xx refused, 5xx or no answer unknown", () => {
    expect(classify(new EmailJSResponseStatus(422, "The recipients address is empty"))).toEqual({
      outcome: "FAILED", status: 422, text: "The recipients address is empty",
    });
    expect(classify(new EmailJSResponseStatus(503, "")).outcome).toBe("UNKNOWN");
    expect(classify(new TypeError("Failed to fetch"))).toEqual({ outcome: "UNKNOWN", status: null, text: "Failed to fetch" });
  });

  it("is configured only when service, template and public key are all set", () => {
    vi.stubEnv("NEXT_PUBLIC_EMAILJS_SERVICE_ID", "service_x");
    vi.stubEnv("NEXT_PUBLIC_EMAILJS_TEMPLATE_ID", "");
    vi.stubEnv("NEXT_PUBLIC_EMAILJS_PUBLIC_KEY", "pk");
    expect(emailJsConfig()).toBeNull();
    expect(missingEmailJsConfig()).toEqual(["NEXT_PUBLIC_EMAILJS_TEMPLATE_ID"]);
    vi.stubEnv("NEXT_PUBLIC_EMAILJS_TEMPLATE_ID", "template_x");
    expect(emailJsConfig()).toEqual({ serviceId: "service_x", templateId: "template_x", publicKey: "pk" });
  });
});
