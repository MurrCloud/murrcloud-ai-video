export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); this.name = "ApiError"; }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/api/v1${path}`, {
      ...init, credentials: "include", cache: "no-store", signal: init.signal ?? controller.signal,
      headers: { ...(init.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...init.headers },
    });
    if (!response.ok) {
      let message = response.status === 401 ? "Your session has expired. Sign in again." : "The request could not be completed. Please retry.";
      try {
        const body = await response.json();
        if (typeof body.detail === "string") message = body.detail;
        else if (Array.isArray(body.detail)) message = body.detail.map((e: { msg?: string }) => e.msg ?? "Invalid input").join(". ");
      } catch { /* Never expose raw server errors or HTML. */ }
      throw new ApiError(response.status, message);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "The studio could not reach the server. Check your connection and retry.");
  } finally { clearTimeout(timeout); }
}

export const money = (amount: string | number | null | undefined) => {
  if (amount === null || amount === undefined || amount === "" || !Number.isFinite(Number(amount))) return "Unavailable";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(amount));
};
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Something went wrong. Please retry.";
