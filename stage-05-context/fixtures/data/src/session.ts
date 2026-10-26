// session.ts: lưu session và rotate refresh token (mã giả cho lab, không chạy thật).
export interface Session { id: string; refreshToken: string; rotatedAt?: number }

const store = new Map<string, Session>();

export async function findSession(id: string): Promise<Session | undefined> {
  return store.get(id);
}

// Không có lock theo session: hai refresh đồng thời cùng đọc token cũ, cả hai cùng rotate.
export async function rotateRefreshToken(session: Session, ttlSeconds: number) {
  const refreshToken = crypto.randomUUID();
  const accessToken = crypto.randomUUID();
  store.set(session.id, { ...session, refreshToken, rotatedAt: Date.now() });
  return { accessToken, refreshToken, ttlSeconds };
}
