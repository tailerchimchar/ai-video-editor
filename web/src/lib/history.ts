/*
 * Undo / redo over whole states — pure, no React. The first `push` is the
 * starting state (nothing to undo yet); each later `push` is one step and
 * clears the redo. `undo` / `redo` return the state to go to, or null.
 */

export interface History<T> {
  push(state: T): void;
  undo(): T | null;
  redo(): T | null;
  readonly canUndo: boolean;
  readonly canRedo: boolean;
}

export function createHistory<T>(limit = 100): History<T> {
  let states: T[] = [];
  let index = -1;
  return {
    push(state) {
      states = [...states.slice(0, index + 1), state].slice(-limit);
      index = states.length - 1;
    },
    undo() {
      if (index <= 0) return null;
      index -= 1;
      return states[index] ?? null;
    },
    redo() {
      if (index >= states.length - 1) return null;
      index += 1;
      return states[index] ?? null;
    },
    get canUndo() {
      return index > 0;
    },
    get canRedo() {
      return index < states.length - 1;
    },
  };
}
