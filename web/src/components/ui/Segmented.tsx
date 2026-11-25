import css from "./ui.module.css";

/** Segmented control; the selected option gets a 1px accent underline as wide as its label. */
export default function Segmented<T extends string>({ options, value, onChange, label, disabled }: {
  options: { value: T; label: string }[];
  value: T | null | undefined;
  onChange: (value: T) => void;
  label: string;
  disabled?: boolean;
}) {
  return <div className={css["seg"]} role="group" aria-label={label}>
    {options.map(o => <button key={o.value} type="button" className={css["opt"]}
      aria-pressed={value === o.value} disabled={disabled} onClick={() => onChange(o.value)}>{o.label}</button>)}
  </div>;
}
