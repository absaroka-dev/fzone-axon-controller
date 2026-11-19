'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { AxonSshClient, hsvToWrgb, wrgbToHsv } = require('../index');

test('uses strict host checking and restricted operations', async () => {
  let observed;
  const runner = (file, args, options, callback) => {
    observed = { file, args, options };
    callback(null, '{"ok":true,"enabled":true}\n');
  };
  const client = new AxonSshClient({ controllerHost: 'axon-light.local' }, runner);
  const response = await client.request('status');
  assert.equal(response.enabled, true);
  assert.equal(observed.file, '/usr/bin/ssh');
  assert.ok(observed.args.includes('StrictHostKeyChecking=yes'));
  assert.equal(observed.args.at(-1), 'status');
});

test('rejects arbitrary commands and values before SSH', async () => {
  const client = new AxonSshClient({ controllerHost: 'axon-light.local' }, () => { throw new Error('runner must not execute'); });
  await assert.rejects(client.request('shell'));
  await assert.rejects(client.request('set-power', 'maybe'));
});

test('allows only validated bounded override commands', async () => {
  let args;
  const runner = (_file, observed, _options, callback) => {
    args = observed;
    callback(null, '{"ok":true,"enabled":true,"channels":[0,35,0,0,0,0,0,0]}\n');
  };
  const client = new AxonSshClient({ controllerHost: 'axon-light.local' }, runner);
  await client.request('override', {
    durationSeconds: 3600, channels: [0, 35, 0, 0, 0, 0, 0, 0],
  });
  assert.deepEqual(args.slice(-3), ['override', '3600', '0,35,0,0,0,0,0,0']);
  await assert.rejects(client.request('override', { durationSeconds: 3600, channels: [101] }));
});

test('maps HomeKit HSV into bounded independent WRGB channels', () => {
  assert.deepEqual(hsvToWrgb(0, 0, 100, 35), [35, 0, 0, 0, 0, 0, 0, 0]);
  assert.deepEqual(hsvToWrgb(0, 100, 100, 35), [0, 35, 0, 0, 0, 0, 0, 0]);
  assert.deepEqual(hsvToWrgb(120, 100, 50, 35), [0, 0, 18, 0, 0, 0, 0, 0]);
  assert.ok(hsvToWrgb(55, 65, 100, 35).every(channel => channel <= 35));
});

test('maps AXON WRGB state back to HomeKit characteristics', () => {
  assert.deepEqual(wrgbToHsv([35, 0, 0, 0, 0, 0, 0, 0], 35), {
    on: true, brightness: 100, hue: 0, saturation: 0,
  });
  assert.deepEqual(wrgbToHsv([0, 0, 35, 0, 0, 0, 0, 0], 35), {
    on: true, brightness: 100, hue: 120, saturation: 100,
  });
});
