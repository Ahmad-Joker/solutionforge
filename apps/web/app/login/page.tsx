"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { Button, ErrorBanner, Field, inputCls } from "@/components/ui";
import { ApiError, postJson } from "@/lib/client";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      if (mode === "register") {
        await postJson("/api/auth/register", {
          email,
          password,
          display_name: name || email.split("@")[0],
        });
      }
      await postJson("/api/auth/login", { email, password });
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto mt-24 max-w-sm rounded-lg border border-slate-200 bg-white p-6">
      <h1 className="mb-1 text-lg font-semibold">SolutionForge</h1>
      <p className="mb-6 text-sm text-slate-500">{mode === "login" ? "Sign in" : "Create an account"}</p>
      <ErrorBanner error={error} />
      <form onSubmit={submit}>
        {mode === "register" && (
          <Field label="Name">
            <input name="name" className={inputCls} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        )}
        <Field label="Email">
          <input
            name="email"
            type="email"
            required
            className={inputCls}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password">
          <input
            name="password"
            type="password"
            required
            minLength={mode === "register" ? 12 : 1}
            className={inputCls}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <div className="mt-4 flex items-center justify-between">
          <Button type="submit" disabled={busy} testId="submit">
            {mode === "login" ? "Sign in" : "Create account"}
          </Button>
          <button
            type="button"
            className="text-sm text-blue-700"
            data-testid="toggle-mode"
            onClick={() => setMode(mode === "login" ? "register" : "login")}
          >
            {mode === "login" ? "Create an account" : "I have an account"}
          </button>
        </div>
      </form>
    </main>
  );
}
