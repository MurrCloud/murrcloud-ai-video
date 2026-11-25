import { ProjectDetails } from "@/components/project-details";
import { StudioShell } from "@/components/studio-shell";
export default async function ProjectPage({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <StudioShell><ProjectDetails id={id}/></StudioShell>; }
