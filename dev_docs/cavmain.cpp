/*! \file cavmain.cpp
 * \brief Cavity::ThreadMain implementation
 * \author Christopher J. Slominski
 */

#include "cavity.h"
#include "zone.h"
#include "linac.h"
#include "const.h"

using namespace cpplib;

/*!
 * The Cavity thread entry point is re-entered each time a gradient Apply
 * operation is initiated. This routine exits with T_Normal when another
 * round is required (FrameThread) and T_Pause to stop the Apply to this
 * Cavity (done or failed).
 */
int Cavity::ThreadMain(void)
{
  // This FrameThread repeats faster than the rate it needs to do work
  // so that it can be responsive to halt requests. Therefore, most
  // executions it simply checks the halt flag and and returns normal
  // status, but will perform its task on major frame executions. Note
  // that this function is not even called when the FrameThread has been
  // paused.
  if (m_halt) return T_Abort;    // Terminate the thread.
  if (++m_count < Const::CavitySubFrames) return T_Normal;
  
  m_count = 0; 
  int status = T_Normal;         // Assume the thread should repeat.

  try
  { // If the Zone has been aborted terminate with Abort status.
    if (m_zone.m_abort)
    { SetView(VW_abort);        // Update GUI's cavity status
      m_evq->Signal(A_abort);   // Tell Zone of completion
      status = T_Pause;         // Stop repeating frames
    }
    
    // LEM does not apply the energy profile to C100 cavities.
    else if (m_zone.m_db.m_type == RFdef::CryoModule::C100)
    { // Download C100 cavity gradient if configured to do so.
      if (Const::Instance().SetC100) m_gset->Put(m_calcGradient);
    
      SetView(VW_unable);        // Icon depends on cfg setting.
      m_evq->Signal(A_success);  // Tell Zone the Apply is done
      status = T_Pause;          // Stop repeating frames
    }

    // Show pause state when the zone is paused from apply action    
    else if (m_zone.m_pause) SetView(VW_pause);
    
    // Is the cavity starting cold? Set tuner to manual and gradient to 2
    // (for non-fixed gradients).
    else if (m_cold == 2)
    { if (!IsFixed())
      { m_gset->Put(Const::Instance().BaseGradient);
        m_rampDone = false;
      }
      m_tuneMode->Put(0);
      m_cold = 1;    
    }
        
    // Can the gradient download proceed (RF must be on), or should the
    // cavity wait for its ZoneMonitor to properly prepare the Zone?
    else if (!m_bypass && m_rfon->Get() == 0) SetView(VW_off);
 
    // Cavities that are not bypassed and do not have broken tuners
    // should have their tuners set to automatic at the beginning of
    // the gradient download.
    else if (m_cold == 1)
    { if (!m_bypass && !m_db.m_tunerBad) m_tuneMode->Put(1);
      m_cold = 0;
    }

    // If finished ramping the gradient, make sure the tuner is happy
    // before declaring this cavity complete. The tuner check is skipped
    // for bypassed cavities or those with broken tuners.
    else if (m_rampDone)
    { if (m_bypass || m_db.m_tunerBad || TunerCheck())
      { SetView(VW_done);          // Update GUI's icon
        m_evq->Signal(A_success);  // Tell Zone the Apply is done
        status = T_Pause;          // Stop repeating frames
      }
    }
          
    // Adjust cavity gradient toward the target value in increments, once
    // per execution of ThreadMain. DownLoad returns true when the final
    // gradient is set.
    else m_rampDone = Download();
  }
  catch (Exception &ex)
  { SetView(VW_fail);                         // Update GUI's cavity status
    m_evq->Signal(A_fail);                    // Tell Zone of completion
    status = T_Pause;                         // Stop repeating frames
    m_zone.m_linac.m_bank.Deposit(ex.what()); // Show diagnostic popup window
  }

  // Is the cavity finished? If so reset variables for a possible next
  // Apply and return thread status such that no more repeats of the
  // frame will occur (until explicitly woken again).
  if (status == T_Pause)
  { m_cold = 1;
    m_rampDone = false;
    m_stepDelay = 0;
  }
  
  return status;  // T_Normal=repeat  T_pause=stop
}


/*!
 * Check the cavity's tuner to see if it is happy. If not, give it some
 * help.
 * \return true if the tuner is happy
 */
bool Cavity::TunerCheck(void)
{
  bool check;

  // Is the detune angle larger than the allowed limit? If so the tuner is
  // not happy.
  if (fabs(m_detuneAngle->Get()) >= Const::Instance().DetuneAngleLimit)
  { check = false;

    // Is the tuner tracking? If not reset the step counter (if large) and
    // occasionally press tuner clear.  
    if (m_tracking->Get() == 0)
    { if (m_stepCount->Get() > 30000) m_stepReset->Put(3);
      else if (m_stepDelay++ > 3)
      { m_stepClear->Put(3);
        m_stepDelay = 0;
      }  
    }
  }
  else
  { m_stepDelay = 0;
    check = true;
  }
  
  return check;
}


/*!
 * Download the calculated gradient to the control system. Follow all rules
 * that restrict this activity.
 * \return True if gradient ramping is done
 */
bool Cavity::Download(void)
{
  double nextGradient;
  T_ViewMode vm;

  // Bypassed or broken tuner cavities will just be forced to their
  // target gradient (they should be there anyway).
  if (m_bypass || m_db.m_tunerBad)
  { nextGradient = m_calcGradient;
    vm = VW_ramp;
  }
  
  // Is the cavity supposed to be dropped to a minimal gradient before
  // ramping to its target gradient?
  else if (m_drop)
  { nextGradient = Const::Instance().BaseGradient;
    m_drop = false;
    vm = VW_ramp;
  }

  // Is the tuner happy? If not, no gradient ramping.
  else if (!TunerCheck())
  { // No new gradient, just set cavity's view to the tuner wait state
    nextGradient = -1;
    vm = VW_tuner;
  }

  // Compute the next gradient step. This may be limited by cryogenic capacity
  // or a predefined slew rate.
  else
  { double cryoSlew = 1.e99; // default to huge, always overridden

    // Only restrict change from cryo concerns if the zone's capacity is still
    // moving UPWARD towards its requested level. Downward ramping doesn't
    // matter.
    if (m_gset->Value() < m_calcGradient)
    { Zone::T_CryoData cryo;
      m_zone.m_cryo.Read(cryo);
      m_cryoGap = cryo.target - cryo.ramp;

      // Has the request been reached (almost)? If so, don't worry about
      // limiting gradient change rate because there should be enough cryo
      // when the requested cryo state has been reached.
      if (m_cryoGap > 0.1)
      { // Compute how much heat energy is available between the current
        // cryo value and the heat required for the zone's current gradients.
        // Don't allow this cavity to take more than the available margin.
        double deltaWatts = cryo.ramp - cryo.load;
        cryoSlew = (deltaWatts <= 0.) ? 0. : sqrt(m_gset->Value() *
          m_gset->Value() + deltaWatts * m_db.LossFactor()) - m_gset->Value();
      }
    }
    
    // Use the base slew rate (m_RFslew) to compute how far the gradient can
    // be changed this cycle. The amount of change decreases with increasing
    // gradient. The slew value is per second, so scale the amount of change
    // to the length of the Apply cycle (milliseconds).
    double slew = ((sqrt(m_gset->Value() * m_gset->Value()
      + Const::Instance().RFslew * Const::Instance().RFslew)
      - m_gset->Value()) * Const::CavityFrame) / 1000.;

    if (slew < cryoSlew) vm = VW_ramp;
    else
    { slew = cryoSlew;
      vm = VW_cryo;
    }    

    // If the allowed change is greater than the gap between the current value
    // and the target, just set to the target value. Otherwise move 'slew'
    // units in the appropriate direction.
    const double gap = m_calcGradient - m_gset->Value();

    if (fabs(gap) <= slew) nextGradient = m_calcGradient;
    else
    { if (gap < 0.) slew = -slew;
      nextGradient = m_gset->Value() + slew;
    }
  }

  // Perform the actions indicated from above, setting the cavity view and
  // possibly downloading a gradient update.
  SetView(vm);
  bool done = false;
  if (nextGradient >= 0.)
  { m_gset->Put(nextGradient);
    done = (m_gset->Value() == m_calcGradient);
  }
  
  return done;
}
