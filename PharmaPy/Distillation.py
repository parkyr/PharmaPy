import numpy as np
from assimulo.problem import Implicit_Problem
from PharmaPy.Phases import classify_phases
from PharmaPy.Streams import VaporStream
from assimulo.solvers import IDA
#Import connectivity

from PharmaPy.Commons import reorder_pde_outputs

import scipy.optimize
import scipy.sparse

class DistillationColumn:
    def __init__(self, name_species, col_P, q_feed, LK, HK, 
                 per_LK, per_HK, reflux=None, num_plates=None, 
                 holdup=None, gamma_model='ideal', N_feed=None):

        self.nomenclature()
        self._Phases = None
        self._Inlet = None
        
        self.num_plates = num_plates
        self.name_species = name_species
        self.reflux = reflux
        self.q_feed = q_feed
        self.col_P = col_P
        self.LK = LK
        self.HK = HK
        self.per_HK = per_HK
        self.per_LK = per_LK
        self.num_species = len(name_species)
        self.M_const = holdup
        self.gamma_model= gamma_model
        self.N_feed=N_feed #Num plate from bottom
        self.per_NLK=100 #Sharp split all NLK recovred in distillate
        self.per_NHK = 0 #Sharp split no NHK in distillate
        return
    
    def nomenclature(self):
        self.name_states = []

    @property
    def Inlet(self):
        return self._Inlet

    @Inlet.setter
    def Inlet(self, inlet):
        self._Inlet = inlet
        self._Inlet.pres = self.col_P
        self.feed_flowrate = inlet.mole_flow
        self.z_feed = inlet.mole_frac

    @property
    def Phases(self):
        return self._Phases

    @Phases.setter
    def Phases(self, phases):
        classify_phases(self)
    
    def estimate_comp (self):
        ### Determine Light Key and Heavy Key component numbers
        # Assume z_feed and name species are in the order- lightest to heaviest
        name_species = self.name_species
        feed_flowrate = self.feed_flowrate
        z_feed = self.z_feed
        LK = self.LK
        HK = self.HK
        LK_index = name_species.index(LK)
        HK_index = name_species.index(HK)
        self.LK_index = LK_index
        self.HK_index = HK_index
        if HK_index != LK_index +1:
            print ('High key and low key indices are not adjacent')
        
        ### Calculate Distillate and Bottom flow rates
        bot_flowrate = (feed_flowrate*z_feed[HK_index]*(1-self.per_HK/100) 
                        + feed_flowrate*z_feed[LK_index]*(1-self.per_LK/100)
                        + sum(feed_flowrate*z_feed[:LK_index])*(1-self.per_NLK/100)
                        + sum(feed_flowrate*z_feed[HK_index+1:])*(1-self.per_NHK/100))
        dist_flowrate = feed_flowrate - bot_flowrate
        
        if bot_flowrate <0 or dist_flowrate<0:
            print('negative flow rates, given value not feasible')
        
        ### Estimate component fractions
        x_dist = np.zeros_like(z_feed)
        x_bot = np.zeros_like(z_feed)
        
        x_bot[:LK_index] = (sum(feed_flowrate*z_feed[:LK_index])
                            *(1-self.per_NLK/100)/bot_flowrate)
        x_bot[LK_index]  = (feed_flowrate*z_feed[LK_index]
                            *(1-self.per_LK/100)/bot_flowrate)
        x_bot[HK_index]  = (feed_flowrate*z_feed[HK_index]
                            *(1-self.per_HK/100)/bot_flowrate)
        x_bot[HK_index+1:] = (sum(feed_flowrate*z_feed[HK_index+1:])
                            *(1-self.per_NHK/100)/bot_flowrate)
        
        x_dist = (feed_flowrate*z_feed - bot_flowrate*x_bot)/dist_flowrate
        
        #Fenske equation
        k_vals_bot = self._Inlet.getKeqVLE(pres = self.col_P, x_liq = x_bot)
        k_vals_dist = self._Inlet.getKeqVLE(pres = self.col_P, x_liq = x_dist)
        alpha_fenske = (k_vals_dist[LK_index]/k_vals_dist[HK_index]*
                        k_vals_bot[LK_index]/k_vals_bot[HK_index])**0.5
        N_min = (np.log(self.per_LK/100/(1-self.per_LK/100)/
                        ((self.per_HK/100)/(1-self.per_HK/100)))
                             /np.log(alpha_fenske))
        self.N_min = N_min
        return x_dist, x_bot, dist_flowrate, bot_flowrate
    
    def get_k_vals(self, x_oneplate=None, temp = None):
        if x_oneplate is None:
            x_oneplate = self.z_feed
        k_vals = self._Inlet.getKeqVLE(pres = self.col_P, temp=temp,
                                       x_liq = x_oneplate)
        return k_vals
        
    def VLE(self, y_oneplate=None, temp = None, need_x_vap=True):
        # VLE uses vapor stream, need vapor stream object temporarily.
        temporary_vapor = VaporStream(path_thermo=self._Inlet.path_data, pres=self.col_P, mole_flow=self.feed_flowrate, mole_frac=y_oneplate)
        res = temporary_vapor.getDewPoint(pres=self.col_P, mass_frac=None, 
                    mole_frac=y_oneplate, thermo_method=self.gamma_model, x_liq=need_x_vap)
        return res[::-1] #Program needs VLE function to return output in x,Temp format
    
    def calc_reflux(self, x_dist = None, x_bot = None, 
                    reflux = None, num_plates = None, pres=None, temp=None):
        ### Calculate operating lines
        #Rectifying section
        if (x_dist is None or x_bot is None):
            x_dist, x_bot, dist_flowrate, bot_flowrate = self.estimate_comp()                                            
        LK_index = self.LK_index
        HK_index = self.HK_index
        k_vals = self.get_k_vals(x_oneplate = self.z_feed, temp=temp) 
        
        alpha = k_vals/k_vals[HK_index]
        ### Estimate Reflux ratio
        # First Underwood equation
        f = lambda phi: (sum(alpha * self.z_feed/(alpha - phi + np.finfo(float).eps)))**2 #(1-q is 0), Feed flow rate cancelled from both sides, 10^-10 is to avoid division by 0
        bounds = ((alpha[HK_index], alpha[LK_index]),)
        phi = scipy.optimize.minimize(f, (alpha[LK_index] + alpha[HK_index])/2, bounds = bounds, tol = 10**-10)
        phi = phi.x
        #Second underwood equation
        V_min = sum(alpha*dist_flowrate*x_dist/(alpha-phi))
        L_min = V_min - dist_flowrate
        min_reflux = L_min/dist_flowrate
        self.min_reflux = min_reflux
        
        if reflux is None:
            reflux = 1.5*min_reflux #Heuristic
        
        if reflux <0:
            reflux = -1*reflux*self.min_reflux 
        
        if num_plates is not None:
            def bot_comp_err (reflux, num_plates, x_dist, x_bot, 
                              dist_flowrate, bot_flowrate):
                
                Ln  = reflux*dist_flowrate
                Vn  = Ln  + dist_flowrate
                
                #Stripping section
                Lm  = Ln + self.feed_flowrate + self.feed_flowrate*(self.q_feed-1) 
                Vm  = Lm  - bot_flowrate
                
                #Calculate compositions
                x = np.zeros((num_plates+1, self.num_species))
                y = np.zeros((num_plates+1, self.num_species))
                T = np.zeros(num_plates+1)
                y[0] = x_dist
                x_new,T_new = self.VLE(y[0])
                x[0] = x_new
                T[0] = T_new
                
                if self.N_feed is None: #Feed plate not specified
                    y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                    y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                    
                    for i in range (1, num_plates+1):
                        #Rectifying section
                        if (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):
                            
                            y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                            x_new,T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new
                            
                            y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                            y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                                
                        #Stripping section
                        else:
                            y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                            x_new,T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new
                
                else: #Feed plate specified
                    for i in range (1, num_plates+1):
                        #Rectifying section
                        if i < num_plates+1 - self.N_feed:
                            y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                            x_new,T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new
                                
                        #Stripping section
                        else:
                            y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                            x_new,T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new
                            
                error = np.linalg.norm(x_bot - x[-1])/np.linalg.norm(x_bot)*100 + 0.01 * reflux**2 #pentalty for very high reflux values
                return error
            
            reflux = scipy.optimize.minimize(bot_comp_err, x0 = 1.5* self.min_reflux, 
                                                 args=(num_plates, x_dist, x_bot, dist_flowrate, bot_flowrate), 
                                                 method = 'Nelder-Mead')
            reflux = reflux.x
        return reflux, x_dist, x_bot, dist_flowrate, bot_flowrate
    
    def calc_plates(self, reflux = None, num_plates = None):
        reflux, x_dist, x_bot, dist_flowrate, bot_flowrate = self.calc_reflux(reflux = reflux, num_plates  = num_plates)
        LK_index = self.LK_index
        HK_index = self.HK_index
        
        ### Calculate Vapour and Liquid flows in column
        #Rectifying section
        Ln  = reflux*dist_flowrate
        Vn  = Ln  + dist_flowrate
        
        #Stripping section
        Lm  = Ln + self.feed_flowrate + self.feed_flowrate*(self.q_feed-1) 
        Vm  = Lm  - bot_flowrate
        
        
        if num_plates is None:
            ### Calculate number of plates
            #Composition list
            x = []
            y = []
            T = []
            counter1= 1
            counter2 =1
            #more likely to have non LKs and no non HKs
            #start counting from top of column
            #First plate
            y.append(x_dist)
            x_new,T_new = self.VLE(y[0])
            x.append(x_new)
            T.append(T_new)
            
            y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
            y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
            
            #Rectifying section
            while (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):
                y.append((np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist))
                x_new,T_new = self.VLE(y[-1])
                x.append(x_new)
                T.append(T_new)
                
                if counter2 >100:
                    break
                counter2 += 1
                y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
            
            #Feed plate
            N_feed = counter2
            
            #Stripping section
            while (np.array(x[-1][HK_index])<0.98*x_bot[HK_index] or np.array(x[-1][LK_index])>1.2*x_bot[LK_index]): #When reflux is specified and num_plates is not calculated x returned contains one extra evaluation, not true for case when num_plates is specified. This is why fudge factors are added
                y.append((np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot))
                x_new,T_new = self.VLE(y[-1])
                x.append(x_new)
                T.append(T_new)
                if counter1 >100:
                    break
                counter1 += 1
                
            num_plates = len(y)-1 #Remove distillate stream, reboiler
            
        else: #Num plates specified
            #Calculate compositions
            x = np.zeros((num_plates+1, self.num_species))
            y = np.zeros((num_plates+1, self.num_species))
            T = np.zeros(num_plates+1)
            
            y[0] = x_dist
            x_new,T_new = self.VLE(y[0])
            x[0] = x_new
            T[0] = T_new
            
            if self.N_feed is None: #Feed plate not specified
                y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                flag_rect_section_present = 0
            
                for i in range (1, num_plates+1):
                    #Rectifying section
                    if (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):
                        
                        y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        x_new,T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                        
                        y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        N_feed = i #To get feed plate, only last value used, cant write outside if cause elif structure will be violated    
                        flag_rect_section_present = 1
                        
                    #Stripping section
                    elif (np.array(x[-1][HK_index])<0.98*x_bot[HK_index] or np.array(x[-1][LK_index])>1.2*x_bot[LK_index]):
                        y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        x_new,T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                        if not(flag_rect_section_present):
                            N_feed = num_plates
            else: #Feed plate specified
                N_feed = self.N_feed
                for i in range (1, num_plates+1):
                    #Rectifying section
                    if i < num_plates+1 - self.N_feed:
                        y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        x_new,T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                        N_feed = self.N_feed
                    #Stripping section
                    else:
                        y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        x_new,T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                                        
        self.N_feed = N_feed
        return num_plates, x, y, T, bot_flowrate, dist_flowrate, reflux, N_feed, x_dist, x_bot, self.min_reflux, self.N_min
    
    
class DynamicDistillation():
    def __init__(self, name_species, col_P, q_feed, LK, HK, 
                 per_LK, per_HK, reflux=None, num_plates=None, 
                 holdup=None, gamma_model='ideal', N_feed=None):
        
        self.nomenclature()
        self._Phases = None
        self._Inlet = None
        
        self.M_const = holdup
        self.num_plates = num_plates
        self.name_species = name_species
        self.reflux = reflux
        self.q_feed = q_feed
        self.col_P = col_P
        self.LK = LK
        self.HK = HK
        self.per_HK = per_HK
        self.per_LK = per_LK
        self.num_species = len(name_species)
        self.LK_index = name_species.index(LK)
        self.HK_index = name_species.index(HK)
        self.gamma_model= gamma_model
        self.N_feed=N_feed #Num plate from bottom
        
        return
    
    def nomenclature(self):
        self.name_states = ['Temp', 'x_liq']

    @property
    def Inlet(self):
        return self._Inlet

    @Inlet.setter
    def Inlet(self, inlet):
        self._Inlet = inlet
        self.feed_flowrate = inlet.mole_flow
        self.z_feed = inlet.mole_frac
        
        #Total reflux conditions (Startup)
        ### Steady state values (Based on steady state column)
        column_user_inputs = {'name_species': self.name_species,
                              'col_P': self.col_P,  # Pa
                              'num_plates': self.num_plates, #exclude reboiler
                              'reflux': self.reflux, #L/D
                              'q_feed': self.q_feed, #Feed q value
                              'LK': self.LK, #LK
                              'HK': self.HK, #HK
                              'per_LK': self.per_LK,# % recovery LK in distillate
                              'per_HK': self.per_HK,# % recovery HK in distillate
                              'holdup': self.M_const,
                              'N_feed': self.N_feed
                              }
        steady_col = DistillationColumn(**column_user_inputs)
        steady_col.Inlet = inlet
        (num_plates, x_ss, y_ss, T_ss, bot_flowrate, dist_flowrate, 
         reflux, N_feed, x_dist, x_bot, min_reflux, N_min) = steady_col.calc_plates(reflux = column_user_inputs['reflux'], 
                                                                                    num_plates = column_user_inputs['num_plates'])
        
        #Calculate compositions
        #For starup Total reflux
        #Total reflux
        x0 = np.zeros_like(x_ss)
        y0 = np.zeros_like(y_ss)
        T0 = np.zeros_like(T_ss)
        column_total_reflux = {'name_species': self.name_species,
                              'col_P': self.col_P,  # Pa
                              'num_plates': num_plates, #exclude reboiler
                              'reflux': 1000, #L/D
                              'q_feed': self.q_feed, #Feed q value
                              'LK': self.LK, #LK
                              'HK': self.HK, #HK
                              'per_LK': self.per_LK,# % recovery LK in distillate
                              'per_HK': self.per_HK,# % recovery HK in distillate
                              'holdup': self.M_const,
                              'N_feed': N_feed
                              }
        
        total_reflux_col = DistillationColumn(**column_total_reflux)
        total_reflux_col.Inlet = inlet
        (num_plates, x0, y0, T0, bot_flowrate, dist_flowrate, 
          reflux, N_feed, x_dist, x_bot, min_reflux, N_min) = total_reflux_col.calc_plates(reflux = column_total_reflux['reflux'], 
                                                                                            num_plates = column_total_reflux['num_plates'])
        x0 = np.array(x0)
        y0 = np.array(y0)
        T0 = np.array(T0)
        T0 = (T0.T).ravel()
        ## Set all values to original steady state
        steady_col = DistillationColumn(**column_user_inputs)
        steady_col.Inlet = inlet
        (num_plates, x_ss, y_ss, T_ss, bot_flowrate, dist_flowrate, 
          reflux, N_feed, x_dist, x_bot, min_reflux, N_min) = steady_col.calc_plates(reflux = column_user_inputs['reflux'], 
                                                                                    num_plates = column_user_inputs['num_plates'])
        self.x0 = x0
        self.y0 = y0
        self.T0 = T0
        self.x_ss = x_ss
        self.y_ss = y_ss
        self.T_ss = T_ss
        self.num_plates = num_plates
        self.bot_flowrate = bot_flowrate
        self.dist_flowrate = dist_flowrate
        self.reflux = reflux
        self.N_feed = N_feed
        self.x_dist = x_dist
        self.x_bot = x_bot
        #min_reflux and N_min are already available as self. 

    @property
    def Phases(self):
        return self._Phases

    @Phases.setter
    def Phases(self, phases):

        classify_phases(self)
    
    def unit_model(self, time, states, d_states):

        '''This method will work by itself and does not need any user manipulation.
        Fill material and energy balances with your model.'''

        states_reord = np.reshape(states, (self.num_plates + 1, self.len_states))
        states_split = [states_reord[:,0], states_reord[:,1:]] #first column in temperature, others are compositions
        dict_states = dict(zip(self.name_states, states_split))
        material = self.material_balances(time, **dict_states)
        
        d_states = np.reshape(d_states, ((self.num_plates + 1, self.len_states)))
        material[:,1:] = material[:,1:] - d_states[:,1:] #N_plates(N_components), only for xompositions
        balances = material.ravel()
        return balances
    
    def material_balances(self, time, x_liq, Temp):
        x = x_liq
        ##GET STARTUP CONDITIONS
        (bot_flowrate, dist_flowrate, 
         reflux, N_feed, M_const) = (self.bot_flowrate, self.dist_flowrate, 
                                     self.reflux, self.N_feed, self.M_const)
        
        #CALCULATE COLUMN FLOWS
        #Rectifying section
        Ln  = reflux*dist_flowrate
        Vn  = Ln  + dist_flowrate
        #Stripping section
        Lm  = Ln + self.feed_flowrate + self.feed_flowrate*(self.q_feed-1) 
        Vm  = Lm  - bot_flowrate
        
        dxdt = np.zeros_like(x)
        p_vap = self._Inlet.AntoineEquation(Temp)
        residuals_temp = (self.col_P - (x*p_vap).sum(axis=1))
        
        k_vals = self._Inlet.getKeqVLE(pres = self.col_P, temp=Temp,
                                       x_liq = x)
        alpha = k_vals/k_vals[self.HK_index]
        
        y = ((alpha*x).T/np.sum(alpha*x,axis=1)).T
        
        #Rectifying section
        dxdt[0] = (1/M_const)*(Vn*y[1] + Ln*y[0] - Vn*y[0] - Ln*x[0]) #Distillate plate
        dxdt[1:N_feed-1] = (1/M_const)*(Vn*y[2:N_feed] + Ln*x[0:N_feed-2] - Vn*y[1:N_feed-1] - Ln*x[1:N_feed-1])
        #Stripping section
        dxdt[N_feed-1] = (1/M_const)*(Vm*y[N_feed] + Ln*x[N_feed-2] + self.feed_flowrate*self.z_feed - Vn*y[N_feed-1] - Lm*x[N_feed-1]) #Feed plate
        dxdt[N_feed:-1] = (1/M_const)*(Vm*y[N_feed+1:] + Lm*x[N_feed-1:-2] -Vm*y[N_feed:-1] - Lm*x[N_feed:-1])
        dxdt[-1] = (1/M_const)*(Vm*x[-1] + Lm*x[-2] -Vm*y[-1] - Lm*x[-1]) #Reboiler, y_in for reboiler is the same as x_out
        mat_bal = np.column_stack((residuals_temp, dxdt))
        return mat_bal
        
    def energy_balances(self, time, x_liq, Temp):
        pass
        return
    
    def solve_model(self, runtime=None, t0=0):
        self.len_states = len(self.name_species) + 1 # 3 compositions + 1 temperature per plate
        
        init_states = np.column_stack((self.T0, self.x0))
        init_derivative = self.material_balances(time=0, x_liq=self.x0, Temp=self.T0)
        
        problem = Implicit_Problem(self.unit_model, init_states.ravel(), init_derivative.ravel(), t0)
        solver = IDA(problem)
        solver.rtol=10**-2
        solver.atol=10**-2
        time, states, d_states = solver.simulate(runtime)
        model_outputs= self.retrieve_results(time, states)
        return time, states, d_states, model_outputs

    def retrieve_results(self, time, states):
        model_outputs = reorder_pde_outputs(states, self.num_plates + 1, np.array([self.len_states]))
        return model_outputs