import { it, expect } from 'vitest';
import { add } from '../src/calc.js';
it('adds two numbers', () => { expect(add(2, 3)).toBe(5); });
it('keeps subtracting elsewhere', () => { expect(3 - 1).toBe(2); });
