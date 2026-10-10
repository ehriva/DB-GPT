type PlanStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

export function summarizeTaskPlan<T extends { status: PlanStatus }>(tasks: readonly T[]) {
  const total = tasks.length;
  const done = tasks.filter(task => task.status === 'completed').length;
  const cancelled = tasks.filter(task => task.status === 'cancelled').length;
  return {
    total,
    done,
    cancelled,
    allDone: total > 0 && done === total,
    currentTask: tasks.find(task => task.status === 'in_progress') || tasks.find(task => task.status === 'pending'),
  };
}
