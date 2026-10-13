"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Trash2, UserPlus } from "lucide-react";
import { useState } from "react";

import { useFormState } from "@/components/auth/use-form-state";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, FormError, Input, Select } from "@/components/ui/field";
import { LoadingState } from "@/components/ui/skeleton";
import { api, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { useInvites, useMembers } from "@/lib/api/queries";
import type { MemberOut, Role } from "@/lib/api/types";
import { inviteSchema, ROLES } from "@/lib/forms";
import { formatDate } from "@/lib/format";

const ROLE_HELP: Record<Role, string> = {
  owner: "Everything, including billing and deleting the organization",
  admin: "Members, API keys, alerts and the audit log",
  member: "Create projects, services and environments",
  viewer: "Read only",
};

function memberError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "last_owner") return "The last owner cannot be demoted or removed. Make someone else an owner first.";
    if (error.code === "forbidden") return error.message ? `${error.message.charAt(0).toUpperCase()}${error.message.slice(1)}.` : describeApiError(error);
    if (error.code === "already_member") return "This person is already a member.";
  }
  return describeApiError(error);
}

export function MembersSettings() {
  const session = useSession();
  const orgId = session.orgId;
  const queryClient = useQueryClient();
  const isAdmin = session.can("admin");
  const isOwner = session.can("owner");
  const members = useMembers(orgId);
  const invites = useInvites(orgId, isAdmin);
  const [actionError, setActionError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [removing, setRemoving] = useState<MemberOut | null>(null);
  const [pending, setPending] = useState(false);
  const myId = session.me?.user.id;
  // Bumped after a sent invitation to remount (clear) the invite form.
  const [inviteFormKey, setInviteFormKey] = useState(0);

  const invalidate = () =>
    Promise.all([queryClient.invalidateQueries({ queryKey: [orgId, "members"] }), queryClient.invalidateQueries({ queryKey: [orgId, "invites"] })]);

  const { bindForm: bindInvite, ...invite } = useFormState(
    inviteSchema,
    async (values) => {
      await unwrap(api.POST("/api/v1/orgs/{org_id}/invites", { params: { path: { org_id: orgId! } }, body: values }));
      setInviteFormKey((k) => k + 1);
      setStatus(`Invitation sent to ${values.email}.`);
      await invalidate();
    },
    { mapError: memberError },
  );

  const changeRole = async (member: MemberOut, role: Role) => {
    setActionError(null);
    setStatus("");
    try {
      await unwrap(
        api.PATCH("/api/v1/orgs/{org_id}/members/{user_id}", { params: { path: { org_id: orgId!, user_id: member.user_id } }, body: { role } }),
      );
      setStatus(`${member.email} is now ${role}.`);
      await queryClient.invalidateQueries({ queryKey: [orgId, "members"] });
      if (member.user_id === myId) await queryClient.invalidateQueries({ queryKey: ["me"] });
    } catch (error) {
      setActionError(memberError(error));
      // Re-read so the select shows the role the server kept.
      await queryClient.invalidateQueries({ queryKey: [orgId, "members"] });
    }
  };

  const remove = async () => {
    if (!removing) return;
    setPending(true);
    setActionError(null);
    try {
      await unwrap(
        api.DELETE("/api/v1/orgs/{org_id}/members/{user_id}", { params: { path: { org_id: orgId!, user_id: removing.user_id } } }),
      );
      setStatus(removing.user_id === myId ? "You left the organization." : `${removing.email} was removed.`);
      if (removing.user_id === myId) await queryClient.invalidateQueries({ queryKey: ["me"] });
      setRemoving(null);
      await invalidate();
    } catch (error) {
      setActionError(memberError(error));
      setRemoving(null);
    } finally {
      setPending(false);
    }
  };

  const revokeInvite = async (inviteId: string, email: string) => {
    setActionError(null);
    try {
      await unwrap(api.DELETE("/api/v1/orgs/{org_id}/invites/{invite_id}", { params: { path: { org_id: orgId!, invite_id: inviteId } } }));
      setStatus(`Invitation for ${email} withdrawn.`);
      await invalidate();
    } catch (error) {
      setActionError(memberError(error));
    }
  };

  if (members.isPending) return <LoadingState label="Loading members…" lines={4} />;
  if (members.isError) return <ApiErrorState what="Members" error={members.error} onRetry={() => void members.refetch()} />;

  return (
    <div className="space-y-6">
      <p role="status" aria-live="polite" className="sr-only">
        {status}
      </p>
      {actionError ? (
        <Callout tone="critical" title="That change was not made">
          {actionError}
        </Callout>
      ) : status ? (
        <Callout tone="healthy">{status}</Callout>
      ) : null}

      <section aria-labelledby="members-title" className="rounded-xl border bg-panel shadow-panel">
        <h2 id="members-title" className="border-b px-5 py-3.5 text-sm font-semibold">
          Members ({members.data.length})
        </h2>
        <ul className="divide-y">
          {members.data.map((m) => {
            const canChange = isAdmin && (isOwner || m.role !== "owner");
            return (
              <li key={m.user_id} className="flex flex-wrap items-center gap-3 px-5 py-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-foreground">
                    {m.display_name || m.email}
                    {m.user_id === myId ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span> : null}
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {m.display_name ? `${m.email} · ` : ""}joined {formatDate(m.joined_at)}
                  </p>
                </div>
                {canChange ? (
                  <>
                    <label htmlFor={`role-${m.user_id}`} className="sr-only">
                      Role of {m.email}
                    </label>
                    <Select
                      id={`role-${m.user_id}`}
                      className="h-8 w-auto text-xs"
                      value={m.role}
                      onChange={(e) => void changeRole(m, e.target.value as Role)}
                    >
                      {ROLES.filter((r) => isOwner || r !== "owner").map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </Select>
                  </>
                ) : (
                  <span className="rounded-md bg-subtle px-2 py-1 text-xs text-foreground-secondary">{m.role}</span>
                )}
                {canChange || m.user_id === myId ? (
                  <Button variant="ghost" size="icon-sm" aria-label={m.user_id === myId ? "Leave the organization" : `Remove ${m.email}`} onClick={() => setRemoving(m)}>
                    <Trash2 aria-hidden="true" />
                  </Button>
                ) : null}
              </li>
            );
          })}
        </ul>
      </section>

      {isAdmin ? (
        <>
          <section aria-labelledby="invite-title" className="rounded-xl border bg-panel p-5 shadow-panel">
            <h2 id="invite-title" className="text-sm font-semibold">
              Invite someone
            </h2>
            <form key={inviteFormKey} ref={bindInvite} onSubmit={invite.onSubmit} noValidate className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_12rem_auto] sm:items-start" aria-label="Invite a member">
              <FormError className="sm:col-span-3">{invite.formError}</FormError>
              <Field label="E-mail" error={invite.errors.email}>
                {(p) => <Input {...p} name="email" type="email" autoComplete="off" />}
              </Field>
              <Field label="Role" error={invite.errors.role} hint={undefined}>
                {(p) => (
                  <Select {...p} name="role" defaultValue="member">
                    {ROLES.filter((r) => isOwner || r !== "owner").map((r) => (
                      <option key={r} value={r}>
                        {r} — {ROLE_HELP[r]}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              <Button type="submit" size="sm" className="sm:mt-7" disabled={invite.pending}>
                <UserPlus aria-hidden="true" />
                {invite.pending ? "Sending…" : "Send invitation"}
              </Button>
            </form>
          </section>

          <section aria-labelledby="invites-title" className="rounded-xl border bg-panel shadow-panel">
            <h2 id="invites-title" className="border-b px-5 py-3.5 text-sm font-semibold">
              Pending invitations
            </h2>
            {invites.isPending ? (
              <LoadingState className="p-5" label="Loading invitations…" />
            ) : invites.isError ? (
              <ApiErrorState what="Invitations" error={invites.error} className="m-4 py-6" />
            ) : invites.data.length === 0 ? (
              <p className="px-5 py-4 text-sm text-muted-foreground">No pending invitations.</p>
            ) : (
              <ul className="divide-y">
                {invites.data.map((i) => (
                  <li key={i.id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm">
                    <span className="min-w-0 flex-1 truncate">{i.email}</span>
                    <span className="text-xs text-muted-foreground">
                      {i.role} · expires {formatDate(i.expires_at)}
                    </span>
                    <Button variant="ghost" size="sm" onClick={() => void revokeInvite(i.id, i.email)}>
                      Withdraw<span className="sr-only"> the invitation for {i.email}</span>
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      ) : (
        <p className="text-sm text-muted-foreground">Owners and admins can invite people and change roles.</p>
      )}

      <ConfirmDialog
        open={removing !== null}
        title={removing?.user_id === myId ? "Leave this organization?" : `Remove ${removing?.email ?? ""}?`}
        confirmLabel={removing?.user_id === myId ? "Leave" : "Remove"}
        pending={pending}
        onConfirm={() => void remove()}
        onClose={() => setRemoving(null)}
      >
        {removing?.user_id === myId
          ? "You lose access immediately. An admin can invite you again."
          : "They lose access immediately. Their past actions stay in the audit log."}
      </ConfirmDialog>
    </div>
  );
}
