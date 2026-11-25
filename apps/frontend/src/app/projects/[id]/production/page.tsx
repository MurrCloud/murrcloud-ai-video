import { ProductionWorkspace } from "@/components/production-workspace";
import { StudioShell } from "@/components/studio-shell";

export default async function ProductionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <StudioShell><ProductionWorkspace projectId={id}/></StudioShell>;
}
