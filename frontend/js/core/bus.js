/* A three-line pub/sub. Panels subscribe to topics; nothing subscribes to a
   transport. That indirection is what lets a panel be driven by a recorded
   frame log in a test. */

const topics = new Map();

export const bus = {
  on(topic, fn) {
    if (!topics.has(topic)) topics.set(topic, new Set());
    topics.get(topic).add(fn);
    return () => topics.get(topic).delete(fn);
  },

  emit(topic, payload) {
    const subs = topics.get(topic);
    if (!subs) return;
    for (const fn of subs) {
      try {
        fn(payload);
      } catch (err) {
        console.error(`[bus] subscriber of "${topic}" threw:`, err);
      }
    }
  },
};
