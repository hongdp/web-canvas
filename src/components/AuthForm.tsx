import React, { useState } from 'react'
import { Shield, Sparkles, Key, User, ArrowRight, Eye, EyeOff } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'

export const AuthForm: React.FC = () => {
  const { login, register } = useAppStore()
  const [isLogin, setIsLogin] = useState<boolean>(true)
  const [username, setUsername] = useState<string>('')
  const [password, setPassword] = useState<string>('')
  const [showPassword, setShowPassword] = useState<boolean>(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState<boolean>(false)

  // Password strength check criteria
  const hasMinLength = password.length >= 8
  const hasUppercase = /[A-Z]/.test(password)
  const hasLowercase = /[a-z]/.test(password)
  const hasDigit = /[0-9]/.test(password)
  const hasSpecial = /[^A-Za-z0-9]/.test(password)

  const passwordStrengthScore = [
    hasMinLength,
    hasUppercase,
    hasLowercase,
    hasDigit,
    hasSpecial
  ].filter(Boolean).length

  const getStrengthLabel = (): string => {
    if (password.length === 0) return ''
    if (passwordStrengthScore <= 2) return 'Weak'
    if (passwordStrengthScore <= 4) return 'Medium'
    return 'Strong'
  }

  const getStrengthColor = (): string => {
    if (passwordStrengthScore <= 2) return '#ef4444' // Red
    if (passwordStrengthScore <= 4) return '#f59e0b' // Orange
    return '#10b981' // Green
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMsg(null)
    setSuccessMsg(null)

    // Validate inputs
    if (!username.trim() || !password) {
      setErrorMsg('Username and password are required.')
      return
    }

    if (!isLogin && password.length < 8) {
      setErrorMsg('Password must be at least 8 characters long.')
      return
    }

    setSubmitting(true)
    try {
      if (isLogin) {
        await login(username, password)
      } else {
        await register(username, password)
        setSuccessMsg('Account registered successfully! You can now log in.')
        setIsLogin(true)
        setPassword('')
      }
    } catch (e) {
      setErrorMsg(e instanceof Error && e.message ? e.message : 'An error occurred during authentication.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div 
      style={{
        display: 'grid',
        placeItems: 'center',
        height: '100%',
        width: '100%',
        backgroundColor: 'var(--bg-primary)',
        color: 'var(--text-primary)',
        fontFamily: 'system-ui, sans-serif',
        padding: '1.5rem',
        overflowY: 'auto',
        boxSizing: 'border-box'
      }}
    >
      <div 
        className="glass-panel"
        style={{
          width: '100%',
          maxWidth: '420px',
          padding: '2.5rem',
          borderRadius: '16px',
          border: '1px solid var(--border-color)',
          display: 'flex',
          flexDirection: 'column',
          gap: '1.5rem',
          boxShadow: '0 20px 40px rgba(0, 0, 0, 0.4)',
          margin: 'auto'
        }}
      >
        {/* Title / Logo header */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center', gap: '0.5rem' }}>
          <div style={{
            width: '48px',
            height: '48px',
            borderRadius: '12px',
            backgroundColor: 'rgba(var(--accent-rgb), 0.15)',
            border: '1px solid var(--border-color)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--accent)',
            marginBottom: '0.25rem'
          }}>
            <Sparkles size={24} />
          </div>
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, margin: 0 }}>Web Canvas</h2>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', margin: 0 }}>
            {isLogin ? 'Log in to sync your collaborative drafts' : 'Register a new account'}
          </p>
        </div>

        {/* Tab Selector */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          backgroundColor: 'var(--bg-tertiary)',
          padding: '4px',
          borderRadius: '8px',
          border: '1px solid var(--border-color)'
        }}>
          <button
            type="button"
            onClick={() => {
              setIsLogin(true)
              setErrorMsg(null)
              setSuccessMsg(null)
            }}
            style={{
              padding: '0.5rem',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: isLogin ? 'var(--bg-secondary)' : 'transparent',
              color: isLogin ? 'var(--text-primary)' : 'var(--text-muted)',
              fontWeight: isLogin ? 600 : 500,
              fontSize: '0.85rem',
              cursor: 'pointer',
              transition: 'all 0.2s'
            }}
          >
            Log In
          </button>
          <button
            type="button"
            onClick={() => {
              setIsLogin(false)
              setErrorMsg(null)
              setSuccessMsg(null)
            }}
            style={{
              padding: '0.5rem',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: !isLogin ? 'var(--bg-secondary)' : 'transparent',
              color: !isLogin ? 'var(--text-primary)' : 'var(--text-muted)',
              fontWeight: !isLogin ? 600 : 500,
              fontSize: '0.85rem',
              cursor: 'pointer',
              transition: 'all 0.2s'
            }}
          >
            Register
          </button>
        </div>

        {/* Form Messages */}
        {errorMsg && (
          <div style={{
            padding: '0.75rem',
            borderRadius: '8px',
            backgroundColor: 'rgba(239, 68, 68, 0.12)',
            border: '1px solid rgba(239, 68, 68, 0.2)',
            color: '#f87171',
            fontSize: '0.8rem',
            lineHeight: 1.4
          }}>
            {errorMsg}
          </div>
        )}

        {successMsg && (
          <div style={{
            padding: '0.75rem',
            borderRadius: '8px',
            backgroundColor: 'rgba(16, 185, 129, 0.12)',
            border: '1px solid rgba(16, 185, 129, 0.2)',
            color: '#34d399',
            fontSize: '0.8rem',
            lineHeight: 1.4
          }}>
            {successMsg}
          </div>
        )}

        {/* Inputs Form */}
        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
          {/* Username */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
            <label htmlFor="auth-username" style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
              Username
            </label>
            <div style={{ position: 'relative' }}>
              <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }}>
                <User size={16} />
              </span>
              <input
                id="auth-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="e.g. writer_bob"
                required
                className="form-input"
                style={{ paddingLeft: '2.25rem', width: '100%' }}
                disabled={submitting}
              />
            </div>
          </div>

          {/* Password */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
            <label htmlFor="auth-password" style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
              Password
            </label>
            <div style={{ position: 'relative' }}>
              <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }}>
                <Key size={16} />
              </span>
              <input
                id="auth-password"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                className="form-input"
                style={{ paddingLeft: '2.25rem', paddingRight: '2.5rem', width: '100%' }}
                disabled={submitting}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                style={{
                  position: 'absolute',
                  right: '10px',
                  top: '50%',
                  transform: 'translateY(-50%)',
                  background: 'none',
                  border: 'none',
                  color: 'var(--text-muted)',
                  cursor: 'pointer',
                  padding: '2px'
                }}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          {/* Registration Password Strength Meter */}
          {!isLogin && password.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', marginTop: '-0.25rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Password Strength:</span>
                <span style={{ fontSize: '0.75rem', fontWeight: 600, color: getStrengthColor() }}>{getStrengthLabel()}</span>
              </div>
              <div style={{
                height: '4px',
                width: '100%',
                backgroundColor: 'var(--bg-tertiary)',
                borderRadius: '2px',
                overflow: 'hidden'
              }}>
                <div style={{
                  height: '100%',
                  width: `${(passwordStrengthScore / 5) * 100}%`,
                  backgroundColor: getStrengthColor(),
                  transition: 'width 0.2s'
                }} />
              </div>
              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px', marginTop: '0.2rem' }}>
                <span style={{ color: hasMinLength ? '#10b981' : 'var(--text-muted)' }}>✔ Min 8 characters</span>
                <span style={{ color: hasUppercase ? '#10b981' : 'var(--text-muted)' }}>✔ Uppercase letter</span>
                <span style={{ color: hasLowercase ? '#10b981' : 'var(--text-muted)' }}>✔ Lowercase letter</span>
                <span style={{ color: hasDigit ? '#10b981' : 'var(--text-muted)' }}>✔ Number digit</span>
                <span style={{ color: hasSpecial ? '#10b981' : 'var(--text-muted)' }}>✔ Special symbol</span>
              </div>
            </div>
          )}

          {/* Submit Button */}
          <button
            type="submit"
            disabled={submitting}
            className="btn-primary"
            style={{
              width: '100%',
              padding: '0.75rem',
              justifyContent: 'center',
              display: 'flex',
              alignItems: 'center',
              gap: '0.5rem',
              fontWeight: 600,
              fontSize: '0.9rem',
              marginTop: '0.5rem'
            }}
          >
            {isLogin ? 'Log In' : 'Sign Up'} <ArrowRight size={16} />
          </button>
        </form>

        <div style={{
          fontSize: '0.75rem',
          color: 'var(--text-muted)',
          textAlign: 'center',
          borderTop: '1px solid var(--border-color)',
          paddingTop: '1rem',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '0.35rem'
        }}>
          <Shield size={12} style={{ color: 'var(--accent)' }} /> 
          Sessions are fully encrypted & secured.
        </div>
      </div>
    </div>
  )
}
