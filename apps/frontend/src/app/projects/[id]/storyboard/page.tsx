import { StoryboardWorkspace } from "@/components/storyboard-workspace";
import { StudioShell } from "@/components/studio-shell";

export default async function StoryboardPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <StudioShell><StoryboardWorkspace projectId={id}/></StudioShell>;
}
