import { describe, expect, it } from "vitest";
import { defaultModel, generationBlockReason, publicClipPrice, settingsForModel, validateSettings } from "../src/lib/model-settings";
import { config, estimate, imageModel, kling, wan } from "./fixtures";

describe("model capability selection", () => {
  it("never selects a disabled default", () => {
    expect(defaultModel([{ ...imageModel, is_default: true }, kling])?.model_id).toBe(kling.model_id);
    expect(defaultModel([imageModel])).toBeUndefined();
  });
  it("resets unsupported settings when the model changes", () => {
    const prior = settingsForModel(wan, { duration_seconds: 2, resolution: "720p", aspect_ratio: "4:3" });
    expect(prior.audio_enabled).toBe(true);
    const next = settingsForModel(kling, prior);
    expect(next).toMatchObject({ model_id: kling.model_id, duration_seconds: 3, resolution: "1080p", aspect_ratio: "16:9" });
    expect(validateSettings(kling, next)).toBeNull();
  });
  it("preserves supported aspect and duration", () => {
    const next = settingsForModel(kling, { duration_seconds: 8, aspect_ratio: "9:16", audio_enabled: false });
    expect(next).toMatchObject({ duration_seconds: 8, aspect_ratio: "9:16", audio_enabled: false });
  });
  it("prefers the catalog's top-level discrete duration contract", () => {
    const discrete = { ...wan, supported_duration_seconds: [5, 10], pricing_details: { ...wan.pricing_details, supported_duration_seconds: [2, 3] } };
    expect(settingsForModel(discrete, { duration_seconds: 8 }).duration_seconds).toBe(10);
    expect(validateSettings(discrete, { ...settingsForModel(discrete), duration_seconds: 8 })).toMatch(/duration/);
    expect(validateSettings(discrete, settingsForModel(discrete, { duration_seconds: 5 }))).toBeNull();
  });
  it.each([
    ["resolution", "4k"], ["aspect_ratio", "2:1"], ["duration_seconds", 1], ["duration_seconds", 16], ["duration_seconds", 5.5], ["mode", "image_to_video"], ["audio_enabled", false],
  ])("rejects unsupported %s=%s", (key, value) => {
    expect(validateSettings(wan, { ...settingsForModel(wan), [key]: value })).not.toBeNull();
  });
  it("rejects disabled and mismatched models", () => {
    expect(validateSettings(imageModel, settingsForModel(imageModel))).not.toBeNull();
    expect(validateSettings(kling, settingsForModel(wan))).not.toBeNull();
  });
  it("uses model rates for resolution and audio, without inventing missing prices", () => {
    expect(publicClipPrice(wan, settingsForModel(wan, { duration_seconds: 10, resolution: "1080p" }))).toBe(1.5);
    expect(publicClipPrice(kling, settingsForModel(kling, { duration_seconds: 10, audio_enabled: false }))).toBeCloseTo(1.12);
    expect(publicClipPrice(kling, settingsForModel(kling, { duration_seconds: 10, audio_enabled: true }))).toBeCloseTo(1.68);
    expect(publicClipPrice({ ...wan, pricing_details: {}, price_usd: null }, settingsForModel(wan))).toBeNull();
  });
});

describe("generation approval guard", () => {
  const reason = (gate = config, quote = estimate, approved = true, price = true, cap = "10", budget = "10", committed = "0") => generationBlockReason(gate, quote, approved, price, cap, budget, committed);
  it("requires the operator gate and configured provider", () => {
    expect(reason({ ...config, paid_generation_enabled: false })).toMatch(/disabled/);
    expect(reason({ ...config, provider_configured: false })).toMatch(/not configured/);
    expect(generationBlockReason(null, estimate, true, true, "10", "10")).toMatch(/disabled/);
  });
  it("requires an estimate and rejects expiration", () => {
    expect(generationBlockReason(config, null, true, true, "10", "10")).toMatch(/Estimate/);
    expect(reason(config, { ...estimate, expires_at: "2020-01-01T00:00:00Z" })).toMatch(/expired/);
    expect(reason(config, { ...estimate, expires_at: "invalid" })).toMatch(/expired/);
  });
  it("enforces the total project cap, accounting for existing reservations", () => {
    expect(reason(config, estimate, true, true, "1", "10", "0.5")).toMatch(/remaining/);
    expect(reason(config, estimate, true, true, "11")).toMatch(/budget/);
    expect(reason(config, estimate, true, true, "NaN")).toMatch(/remaining/);
    expect(reason(config, estimate, true, true, "0")).toMatch(/remaining/);
    expect(reason(config, { ...estimate, total_usd: "0.2" }, true, true, "0.3", "1", "0.1")).toBeNull();
  });
  it("requires spending and public-price acknowledgements", () => {
    expect(reason(config, estimate, false)).toMatch(/Approve/);
    expect(reason(config, estimate, true, false)).toMatch(/Acknowledge/);
    expect(reason()).toBeNull();
    expect(reason(config, { ...estimate, pricing_verified: true }, true, false)).toBeNull();
  });
  it("requires explicit unknown-price approval and a positive uncommitted cap", () => {
    const unknown = { ...estimate, total_usd: null, base_usd: null };
    expect(reason(config, unknown, true, false)).toMatch(/Acknowledge/);
    expect(reason(config, unknown, true, true)).toBeNull();
    expect(reason(config, unknown, true, true, "10", "10", "10")).toMatch(/remaining/);
  });
});
