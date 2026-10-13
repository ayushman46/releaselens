"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useFormState } from "@/components/auth/use-form-state";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Field, FormError, Input } from "@/components/ui/field";
import { api, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { OrgOut } from "@/lib/api/types";
import { orgSchema, slugify } from "@/lib/forms";

/** Name + URL slug. The slug follows the name until the user edits it. */
export function CreateOrgForm({ onCreated, submitLabel = "Create organization" }: { onCreated?: (org: OrgOut) => void; submitLabel?: string }) {
  const queryClient = useQueryClient();
  const session = useSession();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);

  const { bindForm, ...form } = useFormState(
    orgSchema,
    async (values) => {
      const org = await unwrap(api.POST("/api/v1/orgs", { body: values }));
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      session.switchOrg(org.id);
      onCreated?.(org);
    },
    {
      mapError: (error) =>
        error instanceof ApiError && error.status === 409 ? "That URL name is taken. Choose another slug." : null,
    },
  );

  return (
    <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-4" aria-label="Create organization">
      <FormError>{form.formError}</FormError>
      <Field label="Organization name" error={form.errors.name}>
        {(p) => (
          <Input
            {...p}
            name="name"
            autoComplete="organization"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (!slugEdited) setSlug(slugify(e.target.value));
            }}
          />
        )}
      </Field>
      <Field label="URL name (slug)" hint="Lowercase letters, digits and hyphens. Used in links and the API." error={form.errors.slug}>
        {(p) => (
          <Input
            {...p}
            name="slug"
            value={slug}
            spellCheck={false}
            autoCapitalize="none"
            onChange={(e) => {
              setSlug(e.target.value);
              setSlugEdited(true);
            }}
          />
        )}
      </Field>
      <Button type="submit" disabled={form.pending}>
        {form.pending ? "Creating…" : submitLabel}
      </Button>
    </form>
  );
}
