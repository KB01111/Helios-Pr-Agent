import * as React from "react";
import {
  Activity,
  Bot,
  Gauge,
  KeyRound,
  Moon,
  Plug,
  RefreshCw,
  ScanSearch,
  Scale,
  Settings as SettingsIcon,
  Sun,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { checkBackend, fetchQuota, loadConfig, refreshAgents, syncRuns } from "@/lib/actions";
import { useGh, loadRepos } from "@/lib/github";
import { forgetSecrets } from "@/lib/session";
import { DEFAULT_MODEL, useApp, type TabId } from "@/lib/store";
import { AgentsPage } from "@/pages/agents";
import { AutonomyPage } from "@/pages/autonomy";
import { IntegrationsPage } from "@/pages/integrations";
import { ProvidersPage } from "@/pages/providers";
import { QuotaPage } from "@/pages/quota";
import { ReviewPage } from "@/pages/review";
import { RunsPage } from "@/pages/runs";
import { SettingsPage } from "@/pages/settings";

interface TabDef {
  id: TabId;
  label: string;
  title: string;
  sub: React.ReactNode;
  icon: LucideIcon;
  group: "Workspace" | "Operations" | "System";
}

const TABS: TabDef[] = [
  {
    id: "review",
    label: "Review",
    title: "Review",
    icon: ScanSearch,
    group: "Workspace",
    sub: (
      <>
        Model <code>{DEFAULT_MODEL}</code> via OpenRouter · secrets live in memory/session only
      </>
    ),
  },
  {
    id: "agents",
    label: "Agents & sync",
    title: "Agents & repo sync",
    icon: Bot,
    group: "Workspace",
    sub: "Backend agents decide which repos, triggers and runtimes handle each pull request.",
  },
  {
    id: "runs",
    label: "Runs",
    title: "Background runs",
    icon: Activity,
    group: "Workspace",
    sub: "Runs recorded by the backend while this PC was offline.",
  },
  {
    id: "autonomy",
    label: "Autonomy",
    title: "Autonomy & escalations",
    icon: Scale,
    group: "Workspace",
    sub: "How much the decision layer may do without asking you.",
  },
  {
    id: "quota",
    label: "Quota",
    title: "Quota & autoscale",
    icon: Gauge,
    group: "Operations",
    sub: "Sandbox vCPU budgets and the scheduled Upstash Box runner.",
  },
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    icon: Workflow,
    group: "Operations",
    sub: "Install the PR-Agent CLI or drop a zero-host review workflow into a repo.",
  },
  {
    id: "integrations",
    label: "Integrations",
    title: "Integrations",
    icon: Plug,
    group: "Operations",
    sub: "Connect GitHub, Vercel, Upstash and OpenRouter — one click setup per repository.",
  },
  {
    id: "settings",
    label: "Settings",
    title: "Settings",
    icon: SettingsIcon,
    group: "System",
    sub: "Backend connection, GitHub sign-in and session keys.",
  },
];

const GROUPS = ["Workspace", "Operations", "System"] as const;

function AppSidebar() {
  const tab = useApp((s) => s.tab);
  const setTab = useApp((s) => s.setTab);
  const backend = useApp((s) => s.backend);
  const escCount = useApp((s) => s.escCount);
  const theme = useApp((s) => s.theme);
  const setTheme = useApp((s) => s.setTheme);

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2.5 px-2 py-1.5">
          <div className="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-lg">
            <ScanSearch className="size-4" />
          </div>
          <div className="grid leading-tight group-data-[collapsible=icon]:hidden">
            <span className="text-sm font-semibold">PR Agent</span>
            <span className="text-muted-foreground text-[11px]">Control · v0.1</span>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        {GROUPS.map((g) => (
          <SidebarGroup key={g}>
            <SidebarGroupLabel>{g}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {TABS.filter((t) => t.group === g).map((t) => (
                  <SidebarMenuItem key={t.id}>
                    <SidebarMenuButton
                      isActive={tab === t.id}
                      tooltip={t.label}
                      onClick={() => setTab(t.id)}
                    >
                      <t.icon />
                      <span>{t.label}</span>
                    </SidebarMenuButton>
                    {t.id === "autonomy" && escCount > 0 ? (
                      <SidebarMenuBadge aria-label="escalations waiting">{escCount}</SidebarMenuBadge>
                    ) : null}
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <div className="flex items-center gap-2 px-2 text-xs group-data-[collapsible=icon]:justify-center">
          <span
            className={
              "size-2 shrink-0 rounded-full " +
              (backend.ok === true ? "bg-success" : backend.ok === false ? "bg-destructive" : "bg-warning")
            }
          />
          <span className="text-muted-foreground truncate group-data-[collapsible=icon]:hidden">
            Backend: <b className="text-foreground font-medium">{backend.label}</b>
          </span>
        </div>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="Toggle theme"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <Sun /> : <Moon />}
              <span>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton tooltip="Clear session secrets" onClick={forgetSecrets}>
              <KeyRound />
              <span>Clear session secrets</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

function Topbar() {
  const tab = useApp((s) => s.tab);
  const backend = useApp((s) => s.backend);
  const def = TABS.find((t) => t.id === tab) as TabDef;
  const [busy, setBusy] = React.useState(false);

  const reloadAll = async () => {
    setBusy(true);
    try {
      await checkBackend(false);
      await Promise.all([loadConfig(), refreshAgents(), syncRuns(), fetchQuota()]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <header className="bg-background/80 sticky top-0 z-10 flex h-16 shrink-0 items-center gap-3 border-b px-4 backdrop-blur">
      <Tooltip>
        <TooltipTrigger asChild>
          <SidebarTrigger />
        </TooltipTrigger>
        <TooltipContent>Toggle sidebar</TooltipContent>
      </Tooltip>
      <Separator orientation="vertical" className="h-6" />
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-base leading-tight font-semibold">{def.title}</h1>
        <p className="text-muted-foreground truncate text-xs">{def.sub}</p>
      </div>
      <Badge variant={backend.ok === true ? "success" : backend.ok === false ? "danger" : "outline"}>
        backend: {backend.label}
      </Badge>
      <Button variant="outline" size="sm" onClick={reloadAll} disabled={busy}>
        <RefreshCw className={busy ? "animate-spin" : ""} />
        Reload
      </Button>
    </header>
  );
}

export function App() {
  const tab = useApp((s) => s.tab);
  const theme = useApp((s) => s.theme);
  const githubToken = useApp((s) => s.secrets.github);

  // Theme: the `dark` class drives shadcn's CSS variables.
  React.useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    root.style.colorScheme = theme;
  }, [theme]);

  // Boot: pull everything the pages show.
  React.useEffect(() => {
    void checkBackend(false);
    void loadConfig();
    void refreshAgents();
    void syncRuns();
    void fetchQuota();
    if (useApp.getState().secrets.github) void loadRepos();
  }, []);

  // Keep the GitHub login badge honest if the token disappears.
  React.useEffect(() => {
    if (!githubToken) useGh.setState({ login: "" });
  }, [githubToken]);

  return (
    <TooltipProvider delayDuration={200}>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset className="h-svh min-w-0 overflow-hidden">
          <Topbar />
          <main className="flex-1 overflow-y-auto p-4 sm:p-6">
            <ReviewPage active={tab === "review"} />
            <AgentsPage active={tab === "agents"} />
            <RunsPage active={tab === "runs"} />
            <AutonomyPage active={tab === "autonomy"} />
            <QuotaPage active={tab === "quota"} />
            <ProvidersPage active={tab === "providers"} />
            <IntegrationsPage active={tab === "integrations"} />
            <SettingsPage active={tab === "settings"} />
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster theme={theme} position="bottom-right" richColors closeButton />
    </TooltipProvider>
  );
}
