// Optional, isolated SQL integration tests. No network and no production DB.
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
const { PGlite } = await import(process.argv[2]);
const db = new PGlite();
await db.exec(await readFile(new URL('../schema.sql', import.meta.url), 'utf8'));
process.stdout.write('{"ready":true}\n');
for await (const line of createInterface({ input: process.stdin })) {
  try {
    const { sql, params = [] } = JSON.parse(line);
    const result = await db.query(sql, params);
    process.stdout.write(JSON.stringify(result, (_, v) => typeof v === 'bigint' ? v.toString() : v) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ error: String(error) }) + '\n');
  }
}
await db.close();
