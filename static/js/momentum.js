(function (root) {
  // UTC calendar arithmetic keeps daily and Monday-based weekly buckets stable
  // across time zones and daylight-saving changes.
  function momentumPeriods(momentum, step, startMonth, endMonth) {
    const seriesList = [
      ...momentum.series.flatMap((series) => [series, ...(series.children || [])]),
      ...(momentum.outcome_series || []),
    ];
    if (step === "monthly") {
      return momentum.months.flatMap((month, index) => {
        if (month.key < startMonth || month.key > endMonth) return [];
        return [{ ...month, values: Object.fromEntries(
          seriesList.map((series) => [series.key, Number(series.values[index]) || 0]),
        ) }];
      });
    }
    if (!startMonth || !endMonth || startMonth > endMonth) return [];
    const first = new Date(`${startMonth}-01T00:00:00Z`);
    const end = new Date(`${endMonth}-01T00:00:00Z`);
    end.setUTCMonth(end.getUTCMonth() + 1);
    const periodKey = (date) => {
      const bucket = new Date(date);
      if (step === "weekly") bucket.setUTCDate(bucket.getUTCDate() - (bucket.getUTCDay() + 6) % 7);
      return bucket.toISOString().slice(0, 10);
    };
    const formatDate = (key) => new Date(`${key}T00:00:00Z`).toLocaleDateString("en-GB", {
      day: "numeric", month: "short", year: "2-digit", timeZone: "UTC",
    });
    const periods = new Map();
    for (const day = new Date(first); day < end; day.setUTCDate(day.getUTCDate() + 1)) {
      const key = periodKey(day);
      if (!periods.has(key)) periods.set(key, {
        key, label: `${step === "weekly" ? "Week of " : ""}${formatDate(key)}`,
        axisLabel: formatDate(key), active: new Set(),
        values: Object.fromEntries(seriesList.map((series) => [series.key, 0])),
      });
    }
    for (const day of momentum.days || []) {
      if (day.key.slice(0, 7) < startMonth || day.key.slice(0, 7) > endMonth) continue;
      const period = periods.get(periodKey(new Date(`${day.key}T00:00:00Z`)));
      if (!period) continue;
      for (const id of day.active) period.active.add(id);
      for (const series of seriesList) {
        if (series.key !== "active") period.values[series.key] += Number(day[series.key]) || 0;
      }
    }
    return Array.from(periods.values(), ({ active, ...period }) => ({
      ...period, values: { ...period.values, active: active.size },
    }));
  }
  if (typeof module === "object" && module.exports) module.exports = momentumPeriods;
  else root.arfsaMomentumPeriods = momentumPeriods;
})(typeof window === "undefined" ? globalThis : window);
