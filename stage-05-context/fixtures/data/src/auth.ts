// auth.ts: handler POST /api/auth/refresh (mã giả cho lab, không chạy thật).
import { findSession, rotateRefreshToken } from "./session";

export const TOKEN_TTL_SECONDS = Number(process.env.TOKEN_TTL_SECONDS ?? 1800); // tăng từ 900 ngày 2026-10-15

export async function refreshHandler(req: Request): Promise<Response> {
  const { sessionId, refreshToken } = await req.json();
  const session = await findSession(sessionId);
  if (!session || session.refreshToken !== refreshToken) {
    // Hai request refresh đồng thời: request thứ hai thấy token đã bị request thứ nhất rotate.
    return new Response(JSON.stringify({ code: "ERR_TOKEN_REFRESH_RACE" }), { status: 500 });
  }
  const next = await rotateRefreshToken(session, TOKEN_TTL_SECONDS);
  // Response API hiện tại (client mobile v1 phụ thuộc): { accessToken, refreshToken, expiresIn }
  return Response.json({ accessToken: next.accessToken, refreshToken: next.refreshToken, expiresIn: TOKEN_TTL_SECONDS });
}
