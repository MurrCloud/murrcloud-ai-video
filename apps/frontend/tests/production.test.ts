import { describe, expect, it } from "vitest";
import { initialSelections, likelyUpscaledScenes, outputDimensions, privateAssetLink, productionDuration, selectionsComplete, validateMusic, validProductionSettings } from "../src/lib/production";
import { production, productionConfig } from "./production-fixtures";

describe("production snapshot helpers", () => {
  it("requires a retained version belonging to every active scene", () => {
    const selected = initialSelections(production);
    expect(selected).toEqual({ "scene-1": "version-2" });
    expect(selectionsComplete(production.scenes, selected)).toBe(true);
    expect(selectionsComplete(production.scenes, { "scene-1": "foreign-version" })).toBe(false);
    expect(selectionsComplete([], {})).toBe(false);
  });
  it("reports exact export dimensions and duration including optional title cards", () => {
    expect(outputDimensions("16:9")).toEqual({ width: 1920, height: 1080 });
    expect(outputDimensions("9:16")).toEqual({ width: 1080, height: 1920 });
    expect(outputDimensions("1:1")).toEqual({ width: 1080, height: 1080 });
    expect(productionDuration(production.scenes, { ...production.settings, opening_title: "Hello", closing_title: "谢谢", title_duration_seconds: 2 })).toBe(9);
  });
  it("identifies smaller selected sources without claiming unknown dimensions are sufficient", () => {
    expect(likelyUpscaledScenes(production.scenes, { "scene-1": "version-2" }, production.settings)).toEqual([]);
    expect(likelyUpscaledScenes(production.scenes, { "scene-1": "version-1" }, production.settings)).toEqual(production.scenes);
  });
  it("rejects invalid rates, volume, title limits and cross-language voice combinations", () => {
    expect(validProductionSettings(production.settings, productionConfig)).toBe(true);
    for (const changes of [{ speaking_rate: "" }, { speaking_rate: "NaN" }, { speaking_rate: "2.01" }, { speaking_rate: "1.001" }, { music_volume: "" }, { music_volume: 2 }, { title_duration_seconds: 6 }, { opening_title: "x".repeat(201) }, { language: "zh-CN" as const }]) {
      expect(validProductionSettings({ ...production.settings, ...changes }, productionConfig)).toBe(false);
    }
    expect(validProductionSettings({ ...production.settings, narration_enabled: false, burn_subtitles: true }, productionConfig)).toBe(false);
  });
  it("allows speech-free production without a configured voice adapter", () => {
    expect(validProductionSettings({ ...production.settings, narration_enabled: false, burn_subtitles: false }, null)).toBe(true);
  });
  it("checks music file bounds before constructing an upload request", () => {
    expect(validateMusic(new File(["audio"], "theme.MP3"), 1024)).toBeNull();
    expect(validateMusic(new File([], "theme.wav"), 1024)).toMatch(/nonempty/);
    expect(validateMusic(new File(["x".repeat(2048)], "theme.wav"), 1024)).toMatch(/no larger/);
    expect(validateMusic(new File(["x"], "theme.exe"), 1024)).toMatch(/MP3 or WAV/);
  });
  it("accepts private asset routes and rejects remote or traversing preview links", () => {
    expect(privateAssetLink("/api/v1/assets/final-1/preview")).toBe("/api/v1/assets/final-1/preview");
    for (const value of ["https://example.com/video.mp4", "//example.com/video.mp4", "/api/v1/assets/../preview", "/api/v1/assets/id/preview?token=secret", "/api/v1/assets/id/preview/extra"]) expect(privateAssetLink(value)).toBeNull();
  });
});
