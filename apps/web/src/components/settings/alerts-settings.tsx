"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Bell, FolderPlus, Mail, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { useFormState } from "@/components/auth/use-form-state";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { StatusPill } from "@/components/rl/status-pill";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Checkbox, Field, FormError, Input, Select } from "@/components/ui/field";
import { LoadingState } from "@/components/ui/skeleton";
import { api, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { useAlertRules, useProjects } from "@/lib/api/queries";
import type { AlertRuleOut } from "@/lib/api/types";
import { alertRuleSchema, projectSchema, RISK_MIN_BANDS, SEVERITY_MIN_BANDS, slugify } from "@/lib/forms";

function CreateProject({ orgId, onCreated }: { orgId: string; onCreated: (id: string) => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const { bindForm, ...form } = useFormState(
    projectSchema,
    async (values) => {
      const project = await unwrap(api.POST("/api/v1/orgs/{org_id}/projects", { params: { path: { org_id: orgId } }, body: values }));
      await queryClient.invalidateQueries({ queryKey: [orgId, "projects"] });
      onCreated(project.id);
    },
    { mapError: (e) => (e instanceof ApiError && e.status === 409 ? "A project with that slug exists. Choose another." : null) },
  );
  return (
    <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-start" aria-label="Create project">
      <FormError className="sm:col-span-3">{form.formError}</FormError>
      <Field label="Project name" error={form.errors.name}>
        {(p) => <Input {...p} name="name" value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label="Slug" error={form.errors.slug}>
        {(p) => <Input {...p} name="slug" key={slugify(name)} defaultValue={slugify(name)} spellCheck={false} />}
      </Field>
      <Button type="submit" size="sm" className="sm:mt-7" disabled={form.pending}>
        <FolderPlus aria-hidden="true" />
        Create project
      </Button>
    </form>
  );
}

function RuleForm({
  orgId,
  projectId,
  rule,
  onDone,
}: {
  orgId: string;
  projectId: string;
  rule: AlertRuleOut | null;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [state, setState] = useState({
    enabled: rule?.enabled ?? true,
    on_regression: rule?.on_regression ?? true,
    on_risk: rule?.on_risk ?? false,
    removeSlack: false,
  });
  const { bindForm, ...form } = useFormState(
    alertRuleSchema,
    async (values) => {
      const slack = values.slack_webhook_url?.trim();
      const body = {
        name: values.name,
        enabled: values.enabled,
        on_regression: values.on_regression,
        regression_min_band: values.regression_min_band,
        on_risk: values.on_risk,
        risk_min_band: values.risk_min_band,
        email_recipients: values.email_recipients,
        // Write-only: omit to keep the stored webhook, null to remove it.
        ...(slack ? { slack_webhook_url: slack } : state.removeSlack ? { slack_webhook_url: null } : {}),
      };
      if (rule) {
        await unwrap(
          api.PATCH("/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules/{rule_id}", {
            params: { path: { org_id: orgId, project_id: projectId, rule_id: rule.id } },
            body,
          }),
        );
      } else {
        await unwrap(api.POST("/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules", { params: { path: { org_id: orgId, project_id: projectId } }, body }));
      }
      await queryClient.invalidateQueries({ queryKey: [orgId, "alert-rules", projectId] });
      onDone();
    },
    {
      read: (el) => {
        const d = new FormData(el);
        return {
          name: String(d.get("name") ?? ""),
          enabled: state.enabled,
          on_regression: state.on_regression,
          regression_min_band: String(d.get("regression_min_band") ?? "HIGH"),
          on_risk: state.on_risk,
          risk_min_band: String(d.get("risk_min_band") ?? "HIGH"),
          slack_webhook_url: String(d.get("slack_webhook_url") ?? ""),
          email_recipients: String(d.get("email_recipients") ?? ""),
        };
      },
      mapError: (e) => {
        if (e instanceof ApiError && e.code === "invalid_webhook_url") return `That webhook URL was refused: ${e.message}.`;
        if (e instanceof ApiError && e.code === "plan_limit_reached") return "Slack alerts need the Team plan. E-mail alerts work on every plan.";
        return null;
      },
    },
  );

  return (
    <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="mt-4 space-y-4" aria-label={rule ? `Edit ${rule.name}` : "New alert rule"}>
      <FormError>{form.formError}</FormError>
      <Field label="Name" error={form.errors.name}>
        {(p) => <Input {...p} name="name" defaultValue={rule?.name ?? ""} />}
      </Field>
      <fieldset className="space-y-3">
        <legend className="text-sm font-medium">Triggers</legend>
        <div className="flex flex-wrap items-center gap-3">
          <Checkbox
            label="Regression detected"
            checked={state.on_regression}
            onChange={(e) => setState((s) => ({ ...s, on_regression: e.target.checked }))}
            aria-invalid={form.errors.on_regression ? true : undefined}
          />
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            at severity
            <Select name="regression_min_band" defaultValue={rule?.regression_min_band ?? "HIGH"} className="h-7 w-auto text-xs">
              {SEVERITY_MIN_BANDS.map((b) => (
                <option key={b} value={b}>
                  {b.toLowerCase()} or above
                </option>
              ))}
            </Select>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Checkbox label="High pre-deploy risk" checked={state.on_risk} onChange={(e) => setState((s) => ({ ...s, on_risk: e.target.checked }))} />
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            at band
            <Select name="risk_min_band" defaultValue={rule?.risk_min_band ?? "HIGH"} className="h-7 w-auto text-xs">
              {RISK_MIN_BANDS.map((b) => (
                <option key={b} value={b}>
                  {b.toLowerCase()} or above
                </option>
              ))}
            </Select>
          </label>
        </div>
        {form.errors.on_regression ? <p className="text-xs font-medium text-critical">{form.errors.on_regression}</p> : null}
      </fieldset>
      <Field
        label="Slack incoming webhook"
        optional
        error={form.errors.slack_webhook_url}
        hint={
          rule?.slack.configured
            ? `A webhook is stored (${rule.slack.hint ?? "hidden"}). It is never shown again; leave this empty to keep it.`
            : "Write-only: stored encrypted and never shown again. Team plan and up."
        }
      >
        {(p) => <Input {...p} name="slack_webhook_url" type="url" autoComplete="off" placeholder="https://hooks.slack.com/services/…" />}
      </Field>
      {rule?.slack.configured ? (
        <Checkbox label="Remove the stored Slack webhook" checked={state.removeSlack} onChange={(e) => setState((s) => ({ ...s, removeSlack: e.target.checked }))} />
      ) : null}
      <Field label="E-mail recipients" optional error={form.errors.email_recipients} hint="Up to 20 addresses, separated by commas or spaces.">
        {(p) => <Input {...p} name="email_recipients" defaultValue={rule?.email_recipients.join(", ") ?? ""} autoComplete="off" />}
      </Field>
      <Checkbox label="Rule enabled" checked={state.enabled} onChange={(e) => setState((s) => ({ ...s, enabled: e.target.checked }))} />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={form.pending}>
          {form.pending ? "Saving…" : rule ? "Save rule" : "Create rule"}
        </Button>
      </div>
    </form>
  );
}

export function AlertsSettings() {
  const session = useSession();
  const orgId = session.orgId!;
  const queryClient = useQueryClient();
  const isAdmin = session.can("admin");
  const projects = useProjects(orgId);
  const [projectChoice, setProjectChoice] = useState<string | null>(null);
  const projectId = projectChoice ?? projects.data?.[0]?.id;
  const rules = useAlertRules(orgId, projectId);
  const [editing, setEditing] = useState<AlertRuleOut | "new" | null>(null);
  const [deleting, setDeleting] = useState<AlertRuleOut | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (projects.isPending) return <LoadingState label="Loading projects…" lines={4} />;
  if (projects.isError) return <ApiErrorState what="Projects" error={projects.error} onRetry={() => void projects.refetch()} />;

  if (projects.data.length === 0) {
    return (
      <EmptyState
        icon={Bell}
        title="Create a project first"
        description="Alert rules belong to a project (a repository and its services). Members and above can create one."
      >
        {session.can("member") ? <CreateProject orgId={orgId} onCreated={setProjectChoice} /> : <p className="text-sm text-muted-foreground">Ask a member or admin to create a project.</p>}
      </EmptyState>
    );
  }

  const remove = async () => {
    if (!deleting || !projectId) return;
    setPending(true);
    setError(null);
    try {
      await unwrap(
        api.DELETE("/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules/{rule_id}", {
          params: { path: { org_id: orgId, project_id: projectId, rule_id: deleting.id } },
        }),
      );
      await queryClient.invalidateQueries({ queryKey: [orgId, "alert-rules", projectId] });
    } catch (e) {
      setError(describeApiError(e));
    } finally {
      setPending(false);
      setDeleting(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="alert-project" className="mb-1 block text-xs font-medium text-muted-foreground">
            Project
          </label>
          <Select id="alert-project" value={projectId} onChange={(e) => setProjectChoice(e.target.value)} className="h-8 w-auto">
            {projects.data.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        {isAdmin ? (
          <Button size="sm" className="ml-auto" onClick={() => setEditing("new")}>
            <Plus aria-hidden="true" />
            New rule
          </Button>
        ) : null}
      </div>
      {!isAdmin ? <Callout tone="info">Only owners and admins can change alert rules. Webhook URLs are masked for everyone.</Callout> : null}
      {error ? <Callout tone="critical">{error}</Callout> : null}

      {rules.isPending ? (
        <LoadingState label="Loading alert rules…" lines={3} />
      ) : rules.isError ? (
        <ApiErrorState what="Alert rules" error={rules.error} onRetry={() => void rules.refetch()} />
      ) : rules.data.length === 0 ? (
        <EmptyState
          icon={Bell}
          title="No alert rules"
          description="Rules post detected regressions (with their probable origin) and high-risk changes to Slack or e-mail. Each deployment alerts once per rule and channel."
          headingLevel={2}
        />
      ) : (
        <ul className="divide-y rounded-xl border bg-panel shadow-panel">
          {rules.data.map((r) => (
            <li key={r.id} className="flex flex-wrap items-start gap-3 px-5 py-4">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-medium">{r.name}</h2>
                  <StatusPill tone={r.enabled ? "healthy" : "unknown"} label={r.enabled ? "Enabled" : "Paused"} size="sm" />
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {[
                    r.on_regression ? `Regressions at ${r.regression_min_band.toLowerCase()}+` : null,
                    r.on_risk ? `Risk at ${r.risk_min_band.toLowerCase()}+` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || "No triggers"}
                </p>
                <p className="mt-1 flex flex-wrap gap-x-3 text-xs text-foreground-secondary">
                  <span>Slack: {r.slack.configured ? r.slack.hint ?? "configured" : "not set"}</span>
                  <span className="inline-flex items-center gap-1">
                    <Mail aria-hidden="true" className="size-3" />
                    {r.email_recipients.length ? r.email_recipients.join(", ") : "no e-mail recipients"}
                  </span>
                </p>
              </div>
              {isAdmin ? (
                <div className="flex gap-1">
                  <Button variant="ghost" size="xs" onClick={() => setEditing(r)}>
                    <Pencil aria-hidden="true" />
                    Edit<span className="sr-only"> {r.name}</span>
                  </Button>
                  <Button variant="ghost" size="xs" onClick={() => setDeleting(r)}>
                    <Trash2 aria-hidden="true" />
                    Delete<span className="sr-only"> {r.name}</span>
                  </Button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      <Dialog open={editing !== null} onClose={() => setEditing(null)} label={editing === "new" ? "New alert rule" : "Edit alert rule"} className="max-w-xl p-6">
        <h2 className="text-base font-semibold">{editing === "new" ? "New alert rule" : `Edit ${editing?.name ?? ""}`}</h2>
        {editing !== null && projectId ? (
          <RuleForm orgId={orgId} projectId={projectId} rule={editing === "new" ? null : editing} onDone={() => setEditing(null)} />
        ) : null}
      </Dialog>
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? ""}?`}
        confirmLabel="Delete rule"
        pending={pending}
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      >
        No more alerts are sent for this rule. Past notifications stay in the log.
      </ConfirmDialog>
    </div>
  );
}
