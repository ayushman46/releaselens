"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useFormState } from "@/components/auth/use-form-state";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, FormError, Input } from "@/components/ui/field";
import { KeyValueList } from "@/components/ui/key-value";
import { LoadingState } from "@/components/ui/skeleton";
import { api, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { useOrg } from "@/lib/api/queries";
import { orgSchema } from "@/lib/forms";
import { formatDate } from "@/lib/format";

const nameOnly = orgSchema.pick({ name: true });

function limit(v: number | null | undefined, unit: string): string {
  return v === null || v === undefined ? `No fixed limit (${unit})` : `${v} ${unit}`;
}

export function GeneralSettings() {
  const session = useSession();
  const queryClient = useQueryClient();
  const router = useRouter();
  const orgId = session.orgId;
  const org = useOrg(orgId);
  const canEdit = session.can("admin");
  const isOwner = session.can("owner");
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const { bindForm, ...form } = useFormState(nameOnly, async ({ name }) => {
    setSaved(false);
    await unwrap(api.PATCH("/api/v1/orgs/{org_id}", { params: { path: { org_id: orgId! } }, body: { name } }));
    await Promise.all([queryClient.invalidateQueries({ queryKey: [orgId, "org"] }), queryClient.invalidateQueries({ queryKey: ["me"] })]);
    setSaved(true);
  });

  if (org.isPending) return <LoadingState label="Loading the organization…" lines={4} />;
  if (org.isError) return <ApiErrorState what="The organization" error={org.error} onRetry={() => void org.refetch()} />;
  const o = org.data;

  const onDelete = async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await unwrap(api.DELETE("/api/v1/orgs/{org_id}", { params: { path: { org_id: o.id } } }));
      setConfirmDelete(false);
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      router.replace("/app");
    } catch (error) {
      setDeleteError(
        error instanceof ApiError && error.code === "subscription_active"
          ? "Cancel the subscription in Billing first: an organization with a live subscription cannot be deleted."
          : describeApiError(error),
      );
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="space-y-6">
      <section aria-labelledby="profile-title" className="rounded-xl border bg-panel p-5 shadow-panel">
        <h2 id="profile-title" className="text-sm font-semibold">
          Organization profile
        </h2>
        {canEdit ? (
          <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="mt-4 max-w-md space-y-4" aria-label="Organization profile">
            <FormError>{form.formError}</FormError>
            <Field label="Name" error={form.errors.name}>
              {(p) => <Input {...p} name="name" defaultValue={o.name} autoComplete="organization" onChange={() => setSaved(false)} />}
            </Field>
            <div className="flex items-center gap-3">
              <Button type="submit" size="sm" disabled={form.pending}>
                {form.pending ? "Saving…" : "Save"}
              </Button>
              <span role="status" className="text-xs text-muted-foreground">
                {saved ? "Saved." : ""}
              </span>
            </div>
          </form>
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">Only owners and admins can rename the organization.</p>
        )}
        <div className="mt-4 border-t">
          <KeyValueList
            items={[
              { label: "URL name (slug)", value: <code className="font-mono text-xs">{o.slug}</code>, hint: "Fixed after creation." },
              { label: "Organization id", value: <code className="font-mono text-xs break-all">{o.id}</code>, hint: "Sent as X-RL-Org by the dashboard." },
              { label: "Your role", value: o.role ?? "—" },
              { label: "Created", value: formatDate(o.created_at) },
              {
                label: "Plan",
                value: `${o.limits.name}: ${limit(o.limits.max_projects, "projects")}, ${limit(o.limits.max_services, "services")}, ${o.limits.retention_days}-day retention`,
              },
            ]}
          />
        </div>
      </section>

      {isOwner ? (
        <section aria-labelledby="danger-title" className="rounded-xl border border-critical-border p-5">
          <h2 id="danger-title" className="text-sm font-semibold">
            Delete organization
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Deletes its projects, deployments, analyses, keys and alert rules. The audit log is kept. This cannot be undone.
          </p>
          {deleteError ? <Callout tone="critical" className="mt-3">{deleteError}</Callout> : null}
          <Button variant="destructive" size="sm" className="mt-4" onClick={() => setConfirmDelete(true)}>
            Delete {o.name}
          </Button>
          <ConfirmDialog
            open={confirmDelete}
            title={`Delete ${o.name}?`}
            confirmLabel="Delete organization"
            pending={deleting}
            onConfirm={onDelete}
            onClose={() => setConfirmDelete(false)}
          >
            Everything in this organization is deleted for every member. This cannot be undone.
          </ConfirmDialog>
        </section>
      ) : null}
    </div>
  );
}
