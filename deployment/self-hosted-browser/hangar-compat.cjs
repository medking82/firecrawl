'use strict';

// The local manager cannot provide the ZDR-only exits required by v2.11.626.
// Refuse those sessions; for remote Hangar preserve upstream's ZDR argument.
function adaptHangar(source) {
  source = source.replace(/\r\n/g, '\n');
  const before = '  const body = {\n    owner: teamId,';
  const after = '  if (process.env.SELF_HOSTED_BROWSER_SOCKET && options.zeroDataRetention)\n    throw new HangarError(400, "Zero data retention browser sessions are not supported by the local manager.");\n  const body = {\n    owner: teamId,';
  const call = 'hangarBrowserOptions(options)';
  const type = '    recordSession: boolean;\n  },\n): Promise<HangarCreated>';
  if (source.split(before).length !== 2 || source.split(call).length !== 2 || source.split(type).length !== 2)
    throw new Error('Unexpected Hangar overlay source; review compatibility');
  return source.replace(type, '    recordSession: boolean;\n    zeroDataRetention: boolean;\n  },\n): Promise<HangarCreated>')
    .replace(before, after).replace(call, 'hangarBrowserOptions(options, options.zeroDataRetention)');
}
module.exports = {adaptHangar};
