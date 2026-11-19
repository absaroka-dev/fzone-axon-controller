'use strict';

const { execFile } = require('node:child_process');
const PLUGIN = 'homebridge-fzone-axon';
const ACCESSORY = 'FzoneAxon';

const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, Number(value)));

function hsvToWrgb(hue, saturation, brightness, maximum = 35) {
  const h = ((Number(hue) % 360) + 360) % 360;
  const s = clamp(saturation, 0, 100) / 100;
  const level = clamp(brightness, 0, 100) / 100 * maximum;
  const white = Math.round(level * (1 - s));
  const chroma = level * s;
  const sector = h / 60;
  const x = chroma * (1 - Math.abs(sector % 2 - 1));
  let rgb;
  if (sector < 1) rgb = [chroma, x, 0];
  else if (sector < 2) rgb = [x, chroma, 0];
  else if (sector < 3) rgb = [0, chroma, x];
  else if (sector < 4) rgb = [0, x, chroma];
  else if (sector < 5) rgb = [x, 0, chroma];
  else rgb = [chroma, 0, x];
  return [white, ...rgb.map(Math.round), 0, 0, 0, 0];
}

function wrgbToHsv(channels, maximum = 35) {
  const [white, red, green, blue] = channels.map(value => clamp(value, 0, 100));
  const colorMaximum = Math.max(red, green, blue);
  const colorMinimum = Math.min(red, green, blue);
  const delta = colorMaximum - colorMinimum;
  let hue = 0;
  if (delta > 0 && colorMaximum === red) hue = 60 * (((green - blue) / delta) % 6);
  else if (delta > 0 && colorMaximum === green) hue = 60 * ((blue - red) / delta + 2);
  else if (delta > 0) hue = 60 * ((red - green) / delta + 4);
  if (hue < 0) hue += 360;
  const combined = white + colorMaximum;
  return {
    on: channels.some(value => value > 0),
    brightness: Math.round(clamp(combined / maximum * 100, 0, 100)),
    hue: Math.round(hue),
    saturation: combined ? Math.round(colorMaximum / combined * 100) : 0,
  };
}

class AxonSshClient {
  constructor(config, runner = execFile) {
    this.runner = runner;
    this.host = config.controllerHost;
    this.user = config.controllerUser || 'axon-bridge';
    this.key = config.identityFile || '/homebridge/axon_ed25519';
    this.knownHosts = config.knownHostsFile || '/homebridge/axon_known_hosts';
    this.timeoutMs = Math.max(1000, Number(config.timeoutMs || 12000));
  }
  request(operation, value) {
    let command;
    if (['health', 'status'].includes(operation) && value === undefined) command = [operation];
    else if (operation === 'set-power' && ['on', 'off'].includes(value)) command = [operation, value];
    else if (operation === 'override'
      && value && Number.isInteger(value.durationSeconds)
      && Array.isArray(value.channels) && value.channels.length === 8
      && value.channels.every(channel => Number.isInteger(channel) && channel >= 0 && channel <= 100)) {
      command = ['override', String(value.durationSeconds), value.channels.join(',')];
    } else return Promise.reject(new Error('invalid AXON operation'));
    const args = [
      '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
      '-o', `UserKnownHostsFile=${this.knownHosts}`, '-o', 'StrictHostKeyChecking=yes',
      '-o', 'ConnectTimeout=3', '-i', this.key, `${this.user}@${this.host}`, ...command,
    ];
    return new Promise((resolve, reject) => this.runner('/usr/bin/ssh', args,
      { timeout: this.timeoutMs, maxBuffer: 4096 }, (error, stdout) => {
        if (error) return reject(new Error(`AXON SSH failed: ${error.message}`));
        try {
          const response = JSON.parse(stdout.trim());
          if (!response.ok) reject(new Error(response.error || `AXON status ${response.status}`));
          else resolve(response);
        } catch (parseError) { reject(parseError); }
      }));
  }
}

class FzoneAxonAccessory {
  constructor(log, config, api) {
    this.log = log;
    this.Service = api.hap.Service;
    this.Characteristic = api.hap.Characteristic;
    this.name = config.name || 'Aquarium Light';
    this.pollMs = Math.max(5000, Number(config.pollIntervalMs || 15000));
    this.maximum = Math.round(clamp(config.maximumChannelPercent || 35, 1, 35));
    this.overrideSeconds = Math.round(clamp(config.overrideSeconds || 3600, 60, 14400));
    this.client = new AxonSshClient(config);
    this.state = { on: false, brightness: 50, hue: 0, saturation: 0 };
    this.waiters = [];
    this.applyTimer = null;
    this.readPromise = null;
    this.lastReadAt = 0;
    this.service = new this.Service.Lightbulb(this.name);
    this.service.getCharacteristic(this.Characteristic.On)
      .onGet(() => this.getValue('on')).onSet(value => this.setOn(Boolean(value)));
    this.service.getCharacteristic(this.Characteristic.Brightness)
      .onGet(() => this.getValue('brightness')).onSet(value => this.setBrightness(value));
    this.service.getCharacteristic(this.Characteristic.Hue)
      .onGet(() => this.getValue('hue')).onSet(value => this.setColor('hue', value));
    this.service.getCharacteristic(this.Characteristic.Saturation)
      .onGet(() => this.getValue('saturation')).onSet(value => this.setColor('saturation', value));
    this.service.addOptionalCharacteristic(this.Characteristic.StatusFault);
    this.timer = setInterval(() => this.refresh(), this.pollMs);
    this.timer.unref();
    this.refresh();
  }

  async readStatus() {
    if (this.readPromise) return this.readPromise;
    if (Date.now() - this.lastReadAt < 2000) return this.lastResponse;
    this.readPromise = this.client.request('status').then(response => {
      this.lastResponse = response;
      this.lastReadAt = Date.now();
      this.state = wrgbToHsv(response.channels, this.maximum);
      return response;
    }).finally(() => { this.readPromise = null; });
    return this.readPromise;
  }

  async getValue(name) {
    await this.readStatus();
    return this.state[name];
  }

  setOn(enabled) {
    this.state.on = enabled;
    if (enabled && this.state.brightness === 0) this.state.brightness = 50;
    return this.queueOverride();
  }

  setBrightness(value) {
    this.state.brightness = Math.round(clamp(value, 0, 100));
    this.state.on = this.state.brightness > 0;
    return this.queueOverride();
  }

  setColor(name, value) {
    this.state[name] = Math.round(clamp(value, 0, name === 'hue' ? 360 : 100));
    return this.state.on ? this.queueOverride() : Promise.resolve();
  }

  queueOverride() {
    if (this.applyTimer) clearTimeout(this.applyTimer);
    const pending = new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
    this.applyTimer = setTimeout(async () => {
      this.applyTimer = null;
      const waiters = this.waiters.splice(0);
      const channels = this.state.on
        ? hsvToWrgb(this.state.hue, this.state.saturation, this.state.brightness, this.maximum)
        : [0, 0, 0, 0, 0, 0, 0, 0];
      try {
        const response = await this.client.request('override', {
          durationSeconds: this.overrideSeconds, channels,
        });
        this.lastResponse = response;
        this.lastReadAt = Date.now();
        waiters.forEach(waiter => waiter.resolve());
        await this.refresh();
      } catch (error) {
        waiters.forEach(waiter => waiter.reject(error));
      }
    }, 250);
    return pending;
  }

  async refresh() {
    const C = this.Characteristic;
    try {
      this.lastReadAt = 0;
      await this.readStatus();
      this.service.updateCharacteristic(C.On, this.state.on);
      this.service.updateCharacteristic(C.Brightness, this.state.brightness);
      this.service.updateCharacteristic(C.Hue, this.state.hue);
      this.service.updateCharacteristic(C.Saturation, this.state.saturation);
      this.service.updateCharacteristic(C.StatusFault, C.StatusFault.NO_FAULT);
    } catch (error) {
      this.log.error('AXON controller unavailable: %s', error.message);
      this.service.updateCharacteristic(C.StatusFault, C.StatusFault.GENERAL_FAULT);
    }
  }
  getServices() { return [this.service]; }
}

module.exports = api => api.registerAccessory(PLUGIN, ACCESSORY, FzoneAxonAccessory);
module.exports.AxonSshClient = AxonSshClient;
module.exports.FzoneAxonAccessory = FzoneAxonAccessory;
module.exports.hsvToWrgb = hsvToWrgb;
module.exports.wrgbToHsv = wrgbToHsv;
