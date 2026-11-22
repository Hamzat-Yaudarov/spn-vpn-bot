// Optional real PostgreSQL/WASM registry tests. In-memory DB; no live DSN.
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
const { PGlite } = await import(process.argv[2]);
const db = new PGlite();
await db.exec(await readFile(new URL('../ops/partner-platform/schema.sql', import.meta.url), 'utf8'));
process.stdout.write('{"ready":true}\n');
for await (const line of createInterface({ input: process.stdin })) {
  try {
    const { sql, params = [], exec = false } = JSON.parse(line);
    const result = exec ? await db.exec(sql) : await db.query(sql, params);
    process.stdout.write(JSON.stringify(result, (_, value) => typeof value === 'bigint' ? value.toString() : value) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({error: String(error), code: error.code,
      constraint: error.constraint || error.constraint_name || null}) + '\n');
  }
}
await db.close();
