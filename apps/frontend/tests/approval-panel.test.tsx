import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApprovalPanel } from "../src/components/approval-panel";
import { config, estimate, project } from "./fixtures";

describe("paid generation approval", () => {
  it("cannot submit while the server paid gate is disabled", async () => {
    const approve = vi.fn();
    render(<ApprovalPanel estimate={estimate} project={project} config={{ ...config, paid_generation_enabled: false }} busy={false} onApprove={approve}/>);
    for (const checkbox of screen.getAllByRole("checkbox")) await userEvent.click(checkbox);
    const button = screen.getByRole("button", { name: "Generation disabled" });
    expect(button).toBeDisabled(); await userEvent.click(button);
    expect(approve).not.toHaveBeenCalled();
  });
  it("requires both explicit approvals before invoking the submission callback", async () => {
    const approve = vi.fn();
    render(<ApprovalPanel estimate={estimate} project={project} config={config} busy={false} onApprove={approve}/>);
    const button = screen.getByRole("button", { name: /Approve & generate/ });
    expect(button).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I approve this total project/ }));
    expect(button).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge/ }));
    expect(button).toBeEnabled(); await userEvent.click(button);
    expect(approve).toHaveBeenCalledExactlyOnceWith("10", true);
  });
  it("shows unknown prices as unavailable and still requires a capped explicit acknowledgement", async () => {
    const approve = vi.fn();
    render(<ApprovalPanel estimate={{ ...estimate, total_usd: null, base_usd: null }} project={project} config={config} busy={false} onApprove={approve}/>);
    expect(screen.getAllByText("Unavailable")).toHaveLength(2);
    expect(screen.getByText(/Pricing is unavailable/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /I approve this total project/ }));
    expect(screen.getByRole("button", { name: /Approve & generate/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge unavailable/ }));
    await userEvent.click(screen.getByRole("button", { name: /Approve & generate/ }));
    expect(approve).toHaveBeenCalledExactlyOnceWith("10", true);
  });
  it("invalidates spending approval when the cap changes", async () => {
    render(<ApprovalPanel estimate={{ ...estimate, pricing_verified: true }} project={project} config={config} busy={false} onApprove={vi.fn()}/>);
    const approval = screen.getByRole("checkbox", { name: /I approve this total project/ });
    await userEvent.click(approval);
    expect(screen.getByRole("button", { name: /Approve & generate/ })).toBeEnabled();
    const cap = screen.getByRole("spinbutton", { name: /Project spending approval cap/ });
    await userEvent.clear(cap); await userEvent.type(cap, "0.1");
    expect(approval).not.toBeChecked();
    expect(screen.getByRole("button", { name: /Approve & generate/ })).toBeDisabled();
  });
  it("blocks a configured gate if provider credentials are missing", () => {
    render(<ApprovalPanel estimate={estimate} project={project} config={{ ...config, provider_configured: false }} busy={false} onApprove={vi.fn()}/>);
    expect(screen.getByRole("button", { name: /Approve & generate/ })).toBeDisabled();
    expect(screen.getByText("The provider is not configured.")).toBeInTheDocument();
  });
});
