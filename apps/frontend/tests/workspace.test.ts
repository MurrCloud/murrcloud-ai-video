import { describe, expect, it } from "vitest";
import { bytes, localTime, pageLabel, validBudget, validEvidenceReference, validProviderRequestId, validVerifiedAmount, workspaceIssue } from "../src/lib/workspace";
describe("workspace reporting and input boundaries", () => {
  it("accepts an explicit zero draft budget and respects database precision and maximum", () => {
    expect(validBudget("0")).toBe(true); expect(validBudget("10000.0000")).toBe(true);
    for (const invalid of ["", "-1", "10000.0001", "10001", "1.12345", "Infinity", "1e4"]) expect(validBudget(invalid)).toBe(false);
  });
  it("requires bounded explicit amounts and verified provider identifiers/evidence", () => {
    for (const value of ["0", "0.000001", "1000000.000000"]) expect(validVerifiedAmount(value)).toBe(true);
    for (const value of ["", "-1", "1e2", "1000000.000001", "0.0000001"]) expect(validVerifiedAmount(value)).toBe(false);
    expect(validProviderRequestId("12345678-1234-1234-1234-123456789abc")).toBe(true); expect(validProviderRequestId("https://provider.example/request")).toBe(false);
    expect(validEvidenceReference("invoice:123")).toBe(true); expect(validEvidenceReference("  ")).toBe(false); expect(validEvidenceReference("a\nb")).toBe(false);
  });
  it("labels bounded pages without claiming a truncated list is the project total", () => { expect(pageLabel(24, 3, 27)).toBe("25–27 of 27"); expect(pageLabel(0, 0, 0)).toBe("No results"); });
  it("distinguishes unavailable storage from measured zero", () => { expect(bytes(null)).toBe("Unavailable"); expect(bytes(Number.NaN)).toBe("Unavailable"); expect(bytes(0)).toBe("0 B"); expect(bytes(1024**3)).toBe("1.0 GiB"); });
  it("rejects invalid dates and gives useful mutation conflicts", () => { expect(localTime("invalid")).toBe("Unavailable"); expect(workspaceIssue("project_archived")).toContain("Restore"); expect(workspaceIssue("revision_conflict")).toContain("Reload"); });
});
