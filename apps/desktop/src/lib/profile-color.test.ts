import { describe, expect, it } from 'vitest'

import { profileColor, resolveProfileColor } from './profile-color'

describe('functional profile colors', () => {
  it('assigns one stable rail color to every profile in a functional group', () => {
    expect(profileColor('ito_it_director')).toBe('hsl(210 68% 58%)')
    expect(profileColor('ito_watchdog')).toBe('hsl(210 68% 58%)')
    expect(profileColor('fin_finance_director')).toBe('hsl(120 68% 58%)')
    expect(profileColor('fin_financial_analyst')).toBe('hsl(120 68% 58%)')
    expect(profileColor('sls_sales_director')).toBe('hsl(30 68% 58%)')
    expect(profileColor('chief-of-staff')).toBe('hsl(270 68% 58%)')
  })

  it('keeps a user-picked color override ahead of the functional default', () => {
    expect(resolveProfileColor('ito_it_director', { ito_it_director: '#123456' })).toBe('#123456')
  })

  it('keeps the default home profile neutral', () => {
    expect(profileColor('default')).toBeNull()
  })
})
