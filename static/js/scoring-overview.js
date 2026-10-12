document.querySelectorAll('[data-scoring-overview]').forEach((block) => {
  const buttons = block.querySelectorAll('[data-scoring-view]');
  const panels = block.querySelectorAll('[data-scoring-panel]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const view = button.dataset.scoringView;
      buttons.forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
      panels.forEach((panel) => { panel.hidden = panel.dataset.scoringPanel !== view; });
    });
  });
});
