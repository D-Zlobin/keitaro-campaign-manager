import { useState } from 'react'

interface ConfirmButtonProps {
  label: string
  confirmLabel?: string
  question?: string
  onConfirm: () => void
  disabled?: boolean
  className?: string
}

/**
 * Two-step destructive action: the first click arms the button and shows an
 * explicit confirmation; only the second click runs the action. Used for
 * discarding local edits and recovery reloads.
 */
export function ConfirmButton({
  label,
  confirmLabel = 'Confirm',
  question = 'Are you sure?',
  onConfirm,
  disabled = false,
  className,
}: ConfirmButtonProps) {
  const [armed, setArmed] = useState(false)

  // Disarm when the action becomes disabled (derived during render, not in an effect).
  const [wasDisabled, setWasDisabled] = useState(disabled)
  if (disabled !== wasDisabled) {
    setWasDisabled(disabled)
    if (disabled) {
      setArmed(false)
    }
  }

  if (!armed) {
    return (
      <button
        type="button"
        className={className}
        disabled={disabled}
        onClick={() => setArmed(true)}
      >
        {label}
      </button>
    )
  }
  return (
    <span className="confirm-armed">
      <span className="confirm-question">{question}</span>
      <button
        type="button"
        className="confirm-yes"
        onClick={() => {
          setArmed(false)
          onConfirm()
        }}
      >
        {confirmLabel}
      </button>
      <button type="button" className="confirm-no" onClick={() => setArmed(false)}>
        Cancel
      </button>
    </span>
  )
}
