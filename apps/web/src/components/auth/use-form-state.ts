"use client";

import { useEffect, useState, type FormEvent } from "react";
import type { z } from "zod";

import { describeApiError, isApiError } from "@/lib/api/errors";
import { readForm, zodFieldErrors, type FieldErrors } from "@/lib/forms";

/**
 * Small form state machine without a form library: zod validation on submit, field errors from
 * zod or the API (`validation_error` details), one form-level error, a pending flag, and focus
 * moved to the first invalid field (or the form error) after a failed submit.
 */
export function useFormState<S extends z.ZodType>(
  schema: S,
  onValid: (values: z.infer<S>) => Promise<void>,
  options: {
    mapError?: (error: unknown) => string | null;
    /** Builds the values to validate; defaults to the form's fields as strings. */
    read?: (form: HTMLFormElement) => unknown;
  } = {},
) {
  // The form element is kept in state (a callback ref), not a ref object, so callers can read
  // the returned object during render.
  const [formEl, bindForm] = useState<HTMLFormElement | null>(null);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (attempt === 0) return;
    const form = formEl;
    if (!form) return;
    const target = form.querySelector<HTMLElement>('[aria-invalid="true"]') ?? form.querySelector<HTMLElement>('[role="alert"]');
    if (target) {
      if (target.getAttribute("role") === "alert" && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus();
    }
    // Run only after a submit attempt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending) return;
    const raw = options.read ? options.read(event.currentTarget) : readForm(event.currentTarget);
    const parsed = schema.safeParse(raw);
    if (!parsed.success) {
      setErrors(zodFieldErrors(parsed.error));
      setFormError(null);
      setAttempt((n) => n + 1);
      return;
    }
    setErrors({});
    setFormError(null);
    setPending(true);
    try {
      await onValid(parsed.data);
    } catch (error) {
      const fieldErrors = isApiError(error) ? error.fieldErrors : {};
      if (Object.keys(fieldErrors).length > 0) setErrors(fieldErrors);
      setFormError(options.mapError?.(error) ?? describeApiError(error));
      setAttempt((n) => n + 1);
    } finally {
      setPending(false);
    }
  };

  const reset = () => formEl?.reset();

  return { bindForm, reset, errors, setErrors, formError, setFormError, pending, onSubmit };
}
