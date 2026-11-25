import { Suspense } from "react";
import { NewProject } from "@/components/new-project";
import { StudioShell } from "@/components/studio-shell";
export default function CreateProjectPage() { return <StudioShell><Suspense fallback={<p>Loading project settings…</p>}><NewProject/></Suspense></StudioShell>; }
