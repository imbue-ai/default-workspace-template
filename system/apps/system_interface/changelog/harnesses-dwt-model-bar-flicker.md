Fixed the model bar (its harness logo especially) flickering during a turn. The backend can
broadcast the same agents snapshot many times as a turn's transcript churns; the frontend
redrew on each, re-injecting the logo's trusted SVG every time. Two fixes: the agents store
now skips a push byte-identical to the previous one (no redundant re-render), and the harness
logo is isolated in a component that only re-renders when its SVG actually changes. The logo
is static per harness, so it now stays put regardless of live model_choice churn.
