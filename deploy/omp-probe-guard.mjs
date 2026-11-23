import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export default function registerProbeGuard(pi) {
  const folder = process.env.OMP_PROBE_DIR;
  if (!folder) throw new Error('The OMP probe directory is missing.');
  const target = join(folder, 'sample.txt');
  pi.on('tool_call', ({ toolName, input }) => {
    if (existsSync(join(folder, 'allow-edit')) && (input?.path === target || input?.path === 'sample.txt')) {
      if (toolName === 'read') return { input: { path: target, i: 'Read probe file' } };
      if (toolName === 'edit' && input.old_string === 'BRIDGE_EDIT_BEFORE'
          && input.new_string === 'BRIDGE_EDIT_AFTER' && input.replace_all !== true) {
        return { input: { path: target, old_string: input.old_string, new_string: input.new_string, i: 'Edit probe file' } };
      }
    }
    return { block: true, reason: 'The probe allows only reading or editing its disposable sample file.' };
  });
  writeFileSync(join(folder, 'guard-ready'), 'ready\n', { flag: 'wx', mode: 0o600 });
}
