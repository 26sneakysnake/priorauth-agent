// Forwards /api/* to the FastAPI server (PRIORAUTH_API_URL, read at request time), so the browser talks to one
// origin and the API needs no CORS. Only the headers the API uses are forwarded, in both directions; x-forwarded-for
// as set by the platform in front of this server (Vercel sets it to the client address) feeds the demo's per-IP
// rate limit.
export const dynamic = "force-dynamic";

const REQUEST_HEADERS = ["authorization", "content-type", "accept", "idempotency-key", "x-forwarded-for"];
const RESPONSE_HEADERS = ["content-type", "www-authenticate", "location"];

async function forward(request: Request, context: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await context.params;
  const base = process.env.PRIORAUTH_API_URL ?? "http://127.0.0.1:8000";
  const target = `${base}/${path.map(encodeURIComponent).join("/")}${new URL(request.url).search}`;
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const init: RequestInit = { method: request.method, headers, cache: "no-store" };
  if (request.method !== "GET") init.body = await request.arrayBuffer();
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return Response.json({ detail: "the review API is unreachable" }, { status: 502 });
  }
  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { forward as GET, forward as POST };
