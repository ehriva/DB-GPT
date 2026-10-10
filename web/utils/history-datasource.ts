export interface HistoryDatabase { name: string; type?: string }
export interface NamedDatabase { db_name: string; db_type: string }

export function historyDatabase(payload: unknown, humanText: string): HistoryDatabase | null | undefined {
  if (payload && typeof payload === 'object' && 'resource_context' in payload) {
    const resource = (payload as { resource_context?: { database?: unknown } }).resource_context;
    if (!resource || !('database' in resource)) return undefined;
    const db = resource.database;
    if (db === null) return null;
    if (db && typeof db === 'object' && 'name' in db && 'type' in db
      && typeof db.name === 'string' && typeof db.type === 'string' && db.name && db.type) {
      return { name: db.name, type: db.type };
    }
    return undefined;
  }
  const name = humanText.match(/^\[Database: ([^\]\r\n]+)\](?:\s|$)/)?.[1];
  return name ? { name } : undefined;
}

export function matchHistoryDatabase<T extends NamedDatabase>(db: HistoryDatabase | null | undefined, sources?: readonly T[]):
  { status: 'matched' | 'cleared' | 'absent' | 'loading' | 'missing'; database: T | null } {
  if (db === null) return { status: 'cleared', database: null };
  if (db === undefined) return { status: 'absent', database: null };
  if (sources === undefined) return { status: 'loading', database: null };
  const matches = sources.filter(s => s.db_name === db.name && (!db.type || s.db_type === db.type));
  return matches.length === 1 ? { status: 'matched', database: matches[0] } : { status: 'missing', database: null };
}
