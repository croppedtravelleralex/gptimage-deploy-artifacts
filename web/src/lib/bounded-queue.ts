export async function runBounded<T>(
  tasks: Array<() => Promise<T>>,
  concurrency = 4,
): Promise<PromiseSettledResult<T>[]> {
  const limit = Math.max(1, concurrency);
  const results: PromiseSettledResult<T>[] = new Array(tasks.length);
  let nextIndex = 0;

  async function worker() {
    while (true) {
      const current = nextIndex;
      nextIndex += 1;
      if (current >= tasks.length) {
        return;
      }
      try {
        const value = await tasks[current]();
        results[current] = { status: "fulfilled", value };
      } catch (reason) {
        results[current] = { status: "rejected", reason };
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, () => worker()));
  return results;
}
