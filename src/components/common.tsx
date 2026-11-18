import * as React from "react";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/** Label + control + optional hint, stacked. */
export function Field({
  label,
  hint,
  htmlFor,
  className,
  children,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  htmlFor?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      <Label htmlFor={htmlFor} className="text-[13px]">
        {label}
      </Label>
      {children}
      {hint ? <p className="text-muted-foreground text-xs leading-relaxed">{hint}</p> : null}
    </div>
  );
}

export function FieldGrid({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("grid gap-4 sm:grid-cols-2", className)}>{children}</div>;
}

/** Monospace output well for command output and logs. */
export const Console = React.forwardRef<
  HTMLPreElement,
  React.ComponentProps<"pre"> & { tall?: boolean }
>(function Console({ className, tall, ...props }, ref) {
  return (
    <pre
      ref={ref}
      className={cn(
        "bg-muted/40 text-muted-foreground border-border max-w-full overflow-auto rounded-md border px-3 py-2.5 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap",
        tall ? "max-h-80" : "max-h-56",
        className,
      )}
      {...props}
    />
  );
});

export function Hint({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-muted-foreground text-[13px] leading-relaxed", className)} {...props} />;
}

/** Password input with a show/hide toggle. */
export function SecretInput({
  className,
  ...props
}: Omit<React.ComponentProps<"input">, "type">) {
  const [show, setShow] = React.useState(false);
  return (
    <div className="relative">
      <Input
        {...props}
        type={show ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        className={cn("pr-9 font-mono", className)}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="absolute top-1/2 right-1.5 -translate-y-1/2"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? "Hide value" : "Show value"}
      >
        {show ? <EyeOff /> : <Eye />}
      </Button>
    </div>
  );
}

/** Button that shows a spinner while its async handler runs. */
export function AsyncButton({
  onClick,
  children,
  disabled,
  ...props
}: Omit<React.ComponentProps<typeof Button>, "onClick"> & {
  onClick: () => unknown | Promise<unknown>;
}) {
  const [busy, setBusy] = React.useState(false);
  const mounted = React.useRef(true);
  React.useEffect(() => () => void (mounted.current = false), []);
  return (
    <Button
      type="button"
      {...props}
      disabled={disabled || busy}
      onClick={async () => {
        setBusy(true);
        try {
          await onClick();
        } finally {
          if (mounted.current) setBusy(false);
        }
      }}
    >
      {busy ? <Loader2 className="animate-spin" /> : null}
      {children}
    </Button>
  );
}

/** A titled card with optional badge/action in the header. */
export function Section({
  title,
  description,
  action,
  children,
  footer,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("gap-4 py-5", className)}>
      <CardHeader className="gap-1 px-5">
        <CardTitle className="text-[15px]">{title}</CardTitle>
        {description ? <CardDescription className="leading-relaxed">{description}</CardDescription> : null}
        {action ? <CardAction>{action}</CardAction> : null}
      </CardHeader>
      {children ? <CardContent className="grid gap-4 px-5">{children}</CardContent> : null}
      {footer ? <CardFooter className="flex-wrap gap-2 px-5">{footer}</CardFooter> : null}
    </Card>
  );
}

export function ButtonRow({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-wrap items-center gap-2", className)} {...props} />;
}

export function Page({
  active,
  className,
  children,
}: {
  active: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  // Pages stay mounted so long-running flows (device sign-in, setup batches,
  // timers) keep running while the user looks at another tab.
  return (
    <div hidden={!active} className={cn("mx-auto grid w-full max-w-4xl gap-5", className)}>
      {children}
    </div>
  );
}

export function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="bg-muted/40 rounded-md border px-3 py-2">
      <div className="text-muted-foreground text-[11px] tracking-wide uppercase">{label}</div>
      <div className="text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export function StatusDot({ state }: { state: "ok" | "warn" | "err" | "idle" }) {
  const color =
    state === "ok" ? "bg-success" : state === "err" ? "bg-destructive" : state === "warn" ? "bg-warning" : "bg-muted-foreground/50";
  return <span className={cn("inline-block size-2.5 shrink-0 rounded-full", color)} aria-hidden />;
}
