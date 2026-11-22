// Read only vendor application code, never runtime configuration or users.
const fs = require('fs');
const path = require('path');
const root = '/opt/app/dist';
const needles = ['xtls-rprx-vision', 'cleanInboundClients'];
let remaining = 10;
function walk(dir) {
  if (!remaining) return;
  for (const item of fs.readdirSync(dir, {withFileTypes: true})) {
    const name = path.join(dir, item.name);
    if (item.isDirectory()) walk(name);
    if (!item.isFile() || !name.endsWith('.js') || !remaining) continue;
    const code = fs.readFileSync(name, 'utf8');
    for (const needle of needles) {
      let at = code.indexOf(needle);
      while (at >= 0 && remaining) {
        process.stdout.write(name + ': ' + code.slice(Math.max(0, at-350), at+850) + '\n');
        remaining--;
        at = code.indexOf(needle, at + needle.length);
      }
    }
  }
}
if (fs.existsSync(root)) walk(root);
else process.stdout.write('Vendor dist directory not found\n');
