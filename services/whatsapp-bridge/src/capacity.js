import { dirSize } from './fsutil.js';

export class CapacityGuard {
  constructor({ dirs, maxBytes = 1024 ** 3, highRatio = 0.8, lowRatio = 0.6, logger }) {
    this.dirs = dirs;
    this.maxBytes = maxBytes;
    this.highRatio = highRatio;
    this.lowRatio = lowRatio;
    this.logger = logger;
    this.high = false;
  }

  check() {
    const bytes = this.dirs.reduce((sum, dir) => sum + dirSize(dir), 0);
    const ratio = bytes / this.maxBytes;
    let action = 'none';
    if (!this.high && ratio >= this.highRatio) {
      this.high = true;
      action = 'disconnect';
      this.logger.error('queue_capacity_high', { ratio: Number(ratio.toFixed(3)) });
    } else if (this.high && ratio <= this.lowRatio) {
      this.high = false;
      action = 'reconnect';
      this.logger.info('queue_capacity_recovered', { ratio: Number(ratio.toFixed(3)) });
    }
    return { action, bytes, ratio };
  }
}
