"use client";

import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, RotateCw, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";

import { useFormState } from "@/components/auth/use-form-state";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { CodeBlock } from "@/components/ui/code-block";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Checkbox, Field, FormError, Input } from "@/components/ui/field";
import { LoadingState } from "@/components/ui/skeleton";
import { api, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { useApiKeys } from "@/lib/api/queries";
import type { ApiKeyCreated, ApiKeyOut } from "@/lib/api/types";
import { API_KEY_SCOPES, apiKeySchema } from "@/lib/forms";
import { DASH, formatDate, formatRelativeTime } from "@/lib/format";

const SCOPE_HELP: Record<(typeof API_KEY_SCOPES)[number], string> = {
  "deployments:write": "Report deployments from CD (POST /api/v1/deployments)",
  "telemetry:write": "Send OpenTelemetry traces (POST /v1/traces)",
  "risk:write": "Request risk scores from the GitHub Action",
  read: "Read deployments, analyses, services and the service map",
};

/** The secret, once. It is never retrievable again (only its hash is stored). */
function SecretOnce({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const doneRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open onClose={onDone} label={`New key ${created.name}`} className="max-w-xl p-6" initialFocus={doneRef}>
      <h2 className="text-base font-semibold">Copy your new key now</h2>
      <Callout tone="warning" icon={TriangleAlert} className="mt-3">
        This is the only time the full key is shown. Store it in your CI secrets; ReleaseLens keeps only a hash and the prefix{" "}
        <code className="font-mono text-xs">{created.prefix}</code>.
      </Callout>
      <CodeBlock code={created.key} title={created.name} className="mt-4" />
      <p className="mt-3 text-xs text-muted-foreground">Scopes: {created.scopes.join(", ")}</p>
      <div className="mt-5 flex justify-end">
        <Button ref={doneRef} size="sm" onClick={onDone}>
          I stored it
        </Button>
      </div>
    </Dialog>
  );
}

export function ApiKeysSettings() {
  const session = useSession();
  const orgId = session.orgId;
  const queryClient = useQueryClient();
  const isAdmin = session.can("admin");
  const keys = useApiKeys(orgId);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "revoke" | "rotate"; key: ApiKeyOut } | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scopes, setScopes] = useState<string[]>(["deployments:write", "telemetry:write"]);

  const { bindForm, ...form } = useFormState(
    apiKeySchema,
    async (values) => {
      const key = await unwrap(
        api.POST("/api/v1/orgs/{org_id}/api-keys", {
          params: { path: { org_id: orgId! } },
          body: { name: values.name, scopes: values.scopes, expires_at: values.expires_at ? new Date(values.expires_at).toISOString() : null },
        }),
      );
      setCreating(false);
      setCreated(key);
      await queryClient.invalidateQueries({ queryKey: [orgId, "api-keys"] });
    },
    {
      read: (el) => {
        const data = new FormData(el);
        return { name: String(data.get("name") ?? ""), scopes, expires_at: String(data.get("expires_at") ?? "") || undefined };
      },
    },
  );

  const act = async () => {
    if (!confirm) return;
    setPending(true);
    setError(null);
    try {
      if (confirm.kind === "revoke") {
        await unwrap(api.DELETE("/api/v1/orgs/{org_id}/api-keys/{key_id}", { params: { path: { org_id: orgId!, key_id: confirm.key.id } } }));
      } else {
        const rotated = await unwrap(
          api.POST("/api/v1/orgs/{org_id}/api-keys/{key_id}/rotate", { params: { path: { org_id: orgId!, key_id: confirm.key.id } } }),
        );
        setCreated(rotated);
      }
      await queryClient.invalidateQueries({ queryKey: [orgId, "api-keys"] });
    } catch (e) {
      setError(e instanceof ApiError && e.code === "key_revoked" ? "A revoked key cannot be rotated; create a new one." : describeApiError(e));
    } finally {
      setPending(false);
      setConfirm(null);
    }
  };

  const columns: DataTableColumn<ApiKeyOut>[] = [
    { id: "name", header: "Name", rowHeader: true, sortValue: (k) => k.name, cell: (k) => k.name },
    { id: "prefix", header: "Key", cell: (k) => <code className="font-mono text-xs">{k.prefix}…</code> },
    { id: "scopes", header: "Scopes", cell: (k) => <span className="text-xs">{k.scopes.join(", ")}</span> },
    { id: "created", header: "Created", sortValue: (k) => new Date(k.created_at), cell: (k) => formatDate(k.created_at) },
    { id: "used", header: "Last used", sortValue: (k) => (k.last_used_at ? new Date(k.last_used_at) : null), cell: (k) => (k.last_used_at ? formatRelativeTime(k.last_used_at) : "Never") },
    {
      id: "state",
      header: "State",
      cell: (k) =>
        k.revoked_at ? (
          <span className="text-xs text-muted-foreground">Revoked {formatDate(k.revoked_at)}</span>
        ) : k.expires_at ? (
          <span className="text-xs">Expires {formatDate(k.expires_at)}</span>
        ) : (
          <span className="text-xs">Active</span>
        ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: (k) =>
        isAdmin && !k.revoked_at ? (
          <span className="flex gap-1">
            <Button variant="ghost" size="xs" onClick={() => setConfirm({ kind: "rotate", key: k })}>
              <RotateCw aria-hidden="true" />
              Rotate<span className="sr-only"> {k.name}</span>
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setConfirm({ kind: "revoke", key: k })}>
              Revoke<span className="sr-only"> {k.name}</span>
            </Button>
          </span>
        ) : (
          DASH
        ),
    },
  ];

  if (keys.isPending) return <LoadingState label="Loading API keys…" lines={4} />;
  if (keys.isError) return <ApiErrorState what="API keys" error={keys.error} onRetry={() => void keys.refetch()} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm text-muted-foreground">
          Keys authenticate your pipeline, Collector and GitHub Action. Send one as <code className="font-mono text-xs">x-releaselens-key</code>.
        </p>
        {isAdmin ? (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Create key
          </Button>
        ) : null}
      </div>
      {!isAdmin ? <Callout tone="info">Only owners and admins can create, rotate or revoke keys.</Callout> : null}
      {error ? <Callout tone="critical" title="That change was not made">{error}</Callout> : null}
      <DataTable<ApiKeyOut>
        caption="API keys"
        columns={columns}
        rows={keys.data}
        getRowId={(k) => k.id}
        initialSort={{ columnId: "created", direction: "descending" }}
        emptyState={
          <EmptyState
            variant="bare"
            headingLevel={3}
            icon={KeyRound}
            title="No API keys yet"
            description="Create one for each system that sends data: your CD pipeline (deployments:write), your Collector (telemetry:write) and the GitHub Action (risk:write)."
          />
        }
      />

      <Dialog open={creating} onClose={() => setCreating(false)} label="Create API key" className="p-6">
        <h2 className="text-base font-semibold">Create API key</h2>
        <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="mt-4 space-y-4" aria-label="Create API key">
          <FormError>{form.formError}</FormError>
          <Field label="Name" error={form.errors.name} hint="Where it will be used, so you know what breaks if you revoke it.">
            {(p) => <Input {...p} name="name" autoComplete="off" />}
          </Field>
          <fieldset aria-describedby={form.errors.scopes ? "scopes-error" : undefined}>
            <legend className="text-sm font-medium">Scopes</legend>
            <div className="mt-2 space-y-2">
              {API_KEY_SCOPES.map((scope) => (
                <Checkbox
                  key={scope}
                  label={<code className="font-mono text-xs">{scope}</code>}
                  description={SCOPE_HELP[scope]}
                  checked={scopes.includes(scope)}
                  onChange={(e) => setScopes((s) => (e.target.checked ? [...s, scope] : s.filter((x) => x !== scope)))}
                />
              ))}
            </div>
            {form.errors.scopes ? (
              <p id="scopes-error" className="mt-1.5 text-xs font-medium text-critical">
                {form.errors.scopes}
              </p>
            ) : null}
          </fieldset>
          <Field label="Expires" optional error={form.errors.expires_at} hint="Leave empty for a key that lasts until revoked.">
            {(p) => <Input {...p} name="expires_at" type="date" />}
          </Field>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={form.pending}>
              {form.pending ? "Creating…" : "Create key"}
            </Button>
          </div>
        </form>
      </Dialog>

      {created ? <SecretOnce created={created} onDone={() => setCreated(null)} /> : null}

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.kind === "rotate" ? `Rotate ${confirm.key.name}?` : `Revoke ${confirm?.key.name ?? ""}?`}
        confirmLabel={confirm?.kind === "rotate" ? "Rotate key" : "Revoke key"}
        pending={pending}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      >
        {confirm?.kind === "rotate"
          ? "A new key with the same scopes is created and shown once; the old key stops working now. Update every system that uses it."
          : "Requests with this key fail immediately. This cannot be undone."}
      </ConfirmDialog>
    </div>
  );
}
