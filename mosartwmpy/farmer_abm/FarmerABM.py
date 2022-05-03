import logging
import numpy as np
from os import mkdir
import pandas as pd
from pyomo.environ import ConcreteModel, Constraint, maximize, NonNegativeReals, Objective, Set, Param, Set, Var 
from pyomo.opt import SolverFactory
from timeit import default_timer as timer
import xarray as xr

from mosartwmpy import model
from mosartwmpy.utilities.pretty_timer import pretty_timer


class FarmerABM:

    def __init__(self, model: model):
        self.model = model
        self.config = model.config 
        self.processed_years = []

        # Get variables from the config.
        self.demand = self.config.get('water_management.demand.demand')
        self.dependent_cell_index = self.config.get('water_management.reservoirs.dependencies.variables.dependent_cell_index')
        self.latitude = self.config.get('grid.latitude')
        self.longitude = self.config.get('grid.longitude')
        self.mu = self.config.get('water_management.demand.farmer_abm.mu', 0.2)
        self.reservoir_grid_index = self.config.get('water_management.reservoirs.parameters.variables.reservoir_grid_index')
        self.reservoir_id = self.config.get('water_management.reservoirs.parameters.variables.reservoir_id')
        self.time = self.config.get('water_management.demand.time')

        # Get variables from the mosartwmpy output based on the mosartwmpy configuration.
        try:
            self.grid_cell_id = next((o for o in self.config.get('simulation.grid_output') if o.get('variable', '').casefold() == 'id'), None).get('name')
        except Exception as e:
            logging.error(f"Can't find the mosartwmpy output needed for the farmer ABM: simulation grid_output variable id.")
            exit()
        try:
            self.grid_cell_supply = next((o for o in self.config.get('simulation.output') if o.get('variable', '').casefold() == 'grid_cell_supply'), None).get('name')
        except Exception as e:
            logging.error(f"Can't find the mosartwmpy output needed for the farmer ABM: simulation output variable grid_cell_supply.")
            exit()
        try:
            self.nldas_id = next((o for o in self.config.get('simulation.grid_output') if o.get('variable', '').casefold() == 'nldas_id'), None).get('name')
        except Exception as e:
            logging.error(f"Can't find the mosartwmpy output needed for the farmer ABM: simulation grid_output variable nldas_id.")
            exit()
        try:
            self.reservoir_storage = next((o for o in self.config.get('simulation.output') if o.get('variable', '').casefold() == 'reservoir_storage'), None).get('name')
        except Exception as e:
            logging.error(f"Can't find the mosartwmpy output needed for the farmer ABM: simulation output variable reservoir_storage.")
            exit()
        try:
            self.runoff_land = next((o for o in self.config.get('simulation.output') if o.get('variable', '').casefold() == 'runoff_land'), None).get('name')
        except Exception as e:
            logging.error(f"Can't find the mosartwmpy output needed for the farmer ABM: simulation output variable runoff_land.")
            exit()
        

    def calc_demand(self):
        t = timer()
        month = '01'
        year = self.model.current_time.year
        # Conversion from acre-feet/year to cubic meters/sec (the demand units that MOSART-WM takes in).
        ACREFTYEAR_TO_CUBICMSEC = 25583.64

        # All file paths.
        dependency_database_path = self.config.get('water_management.reservoirs.dependencies.path')
        historic_storage_supply_path = f"{self.config.get('water_management.demand.farmer_abm.historic_storage_supply.path')}"
        land_water_constraints_by_farm_live_path = f"{self.config.get('water_management.demand.farmer_abm.land_water_constraints_live.path')}"
        land_water_constraints_by_farm_path = self.config.get('water_management.demand.farmer_abm.land_water_constraints.path')
        mosart_wm_pmp_path = f"{self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.path')}"
        output_dir = f"{self.config.get('water_management.demand.output.path')}"
        reservoir_parameter_path = self.config.get('water_management.reservoirs.parameters.path')
        simulation_output_path = f"{self.config.get('simulation.output_path')}/{self.config.get('simulation.name')}/{self.config.get('simulation.name')}_{year}_*.nc"

        # Check we haven't already performed the farmer ABM calculation.
        if year in self.processed_years:
            logging.info(f"Already performed farmer ABM calculations for {year}. Files are in {output_dir}.")
            return

        try:
            warmup_year = self.config.get('simulation.start_date').year + self.config.get('water_management.demand.farmer_abm.warmup_period')

            # During the warm-up period and the first year after the warm-up, read initial values (not live).
            # After that, read off the `live` file.
            if year > warmup_year:
                land_water_constraints_by_farm = pd.read_parquet(land_water_constraints_by_farm_live_path)
            else:
                land_water_constraints_by_farm = pd.read_parquet(land_water_constraints_by_farm_path)

            # If within the warm-up period, use the external baseline water demand files.
            if year < warmup_year:
                water_constraints_by_farm = land_water_constraints_by_farm[[self.config.get('water_management.demand.farmer_abm.land_water_constraints.variables.nldas_id'), self.config.get('water_management.demand.farmer_abm.land_water_constraints.variables.sw_irrigation_vol')]]
                water_constraints_by_farm = water_constraints_by_farm[self.config.get('water_management.demand.farmer_abm.land_water_constraints.variables.sw_irrigation_vol')].to_dict()
            else:
                # ABM constants 
                DEMAND_FACTOR = 'demand_factor'
                STORAGE_SUM = 'STORAGE_SUM'
                STORAGE_SUM_ORIGINAL = 'STORAGE_SUM_OG'
                SW_AVAIL_BIAS_CORRECTION = 'sw_avail_bias_corr'
                WRM_SUPPLY_ORIGINAL = 'WRM_SUPPLY_acreft_OG'
                WRM_SUPPLY_PREV = 'WRM_SUPPLY_acreft_prev'
                WRM_SUPPLY_NEW = 'WRM_SUPPLY_acreft_newinfo'
                WRM_SUPPLY_UPDATED = 'WRM_SUPPLY_acreft_updated'
                WRM_SUPPLY_BIAS_CORRECTION = 'WRM_SUPPLY_acreft_bias_corr'
                RIVER_DISCHARGE_OVER_LAND_ORIGINAL = 'RIVER_DISCHARGE_OVER_LAND_LIQ_OG'

                # Map between grid cell ID and the cell that is dependent upon it (many to many). 
                historic_storage_supply = pd.read_parquet(historic_storage_supply_path)

                # Relationships between grid cells and reservoirs they can consume from (many to many).
                dependency_database = pd.read_parquet(dependency_database_path)

                # Determines which grid cells the reservoirs are located in (one to one).
                reservoir_parameters = xr.open_dataset(reservoir_parameter_path)[[self.reservoir_id, self.reservoir_grid_index]].to_dataframe()

                # Get mosartwmpy output.
                simulation_output = xr.open_mfdataset(simulation_output_path)[[
                    self.grid_cell_id, self.reservoir_storage, self.grid_cell_supply, self.runoff_land, self.nldas_id
                ]].mean('time').to_dataframe().reset_index()

                # Merge the dependencies with the reservoir grid cells.
                dependency_database = dependency_database.merge(reservoir_parameters, how='left', on=self.reservoir_id).rename(columns={self.reservoir_grid_index: self.config.get('water_management.reservoirs.dependencies.variables.reservoir_cell_index')})

                # Merge the dependency database with the mean storage at reservoir locations, and aggregate per grid cell.
                abm_data = dependency_database.merge(simulation_output[[
                    self.grid_cell_id, self.reservoir_storage
                ]], how='left', left_on=self.config.get('water_management.reservoirs.dependencies.variables.reservoir_cell_index'), right_on=self.grid_cell_id).groupby(self.dependent_cell_index, as_index=False)[[self.reservoir_storage]].sum().rename(
                    columns={self.reservoir_storage: STORAGE_SUM}
                )

                # Merge in the mean supply and mean channel outflow from the simulation results per grid cell.
                abm_data[[ 
                    self.grid_cell_supply, self.runoff_land
                ]] =  abm_data[[self.dependent_cell_index]].merge(simulation_output[[
                    self.grid_cell_id, self.grid_cell_supply, self.runoff_land
                ]], how='left', left_on=self.dependent_cell_index, right_on=self.grid_cell_id)[[
                    self.grid_cell_supply, self.runoff_land
                ]]

                # Merge in NLDAS ID from simulation output.
                abm_data = simulation_output[[
                    self.grid_cell_supply, self.nldas_id
                ]].merge(abm_data, left_on=self.grid_cell_supply, right_on=self.dependent_cell_index, how='left')
                    
                # Merge bias correction, original supply in acreft, historic storage, and original channel outflow.
                abm_data[[
                    SW_AVAIL_BIAS_CORRECTION, WRM_SUPPLY_ORIGINAL, RIVER_DISCHARGE_OVER_LAND_ORIGINAL, STORAGE_SUM_ORIGINAL
                ]] = abm_data[[self.nldas_id]].merge(historic_storage_supply[[
                    self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.nldas_id'),
                    self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.sw_avail_bias_correction'),
                    self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.wrm_supply_original'),
                    self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.river_discharge_over_land_liquid_original'),
                    self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.storage_sum_original'),
                ]], left_on=self.nldas_id, right_on=self.config.get('water_management.demand.farmer_abm.historic_storage_supply.variables.nldas_id'), how='left')[[
                    SW_AVAIL_BIAS_CORRECTION, WRM_SUPPLY_ORIGINAL, RIVER_DISCHARGE_OVER_LAND_ORIGINAL, STORAGE_SUM_ORIGINAL
                ]]

                # Select only the NLDAS_IDs listed in historic_storage_supply.
                abm_data = abm_data.loc[abm_data[self.nldas_id].isin(historic_storage_supply[self.nldas_id])]

                # Rename original supply.
                abm_data[WRM_SUPPLY_PREV] = abm_data[WRM_SUPPLY_ORIGINAL]

                # Zero the missing data.
                abm_data = abm_data.fillna(0)

                # Calculate a "demand factor" for each agent.
                abm_data[DEMAND_FACTOR] = np.where(
                    abm_data[STORAGE_SUM_ORIGINAL] > 0,
                    abm_data[STORAGE_SUM] / abm_data[STORAGE_SUM_ORIGINAL],
                    np.where(
                        abm_data[RIVER_DISCHARGE_OVER_LAND_ORIGINAL] >= 0.1,
                        abm_data[self.runoff_land] / abm_data[RIVER_DISCHARGE_OVER_LAND_ORIGINAL],
                        1
                    )
                )

                abm_data[WRM_SUPPLY_NEW] = abm_data[DEMAND_FACTOR] * abm_data[WRM_SUPPLY_ORIGINAL]
                abm_data[WRM_SUPPLY_UPDATED] = ((1 - self.mu) * abm_data[WRM_SUPPLY_PREV]) + (self.mu * abm_data[WRM_SUPPLY_NEW])
                abm_data[WRM_SUPPLY_PREV] = abm_data[WRM_SUPPLY_UPDATED]

                # Update parquet with 'live' data: variables updated year to year.
                abm_data[[self.nldas_id, WRM_SUPPLY_ORIGINAL, WRM_SUPPLY_PREV, SW_AVAIL_BIAS_CORRECTION, DEMAND_FACTOR, RIVER_DISCHARGE_OVER_LAND_ORIGINAL]].to_parquet(land_water_constraints_by_farm_live_path)

                abm_data[WRM_SUPPLY_BIAS_CORRECTION] = abm_data[WRM_SUPPLY_UPDATED] + abm_data[SW_AVAIL_BIAS_CORRECTION]

                water_constraints_by_farm = abm_data.reset_index()[WRM_SUPPLY_BIAS_CORRECTION].to_dict()
                logging.info(f"Converted units dataframe for month {month} year {year}")

            logging.info(f"Loaded water availability files for month {month} year {year}.")

            # Read in PMP calibration files.
            mosart_wm_pmp = pd.read_parquet(mosart_wm_pmp_path)
            nirs = mosart_wm_pmp[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nir_corrected')].to_dict()
            gammas = mosart_wm_pmp[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.gammas')].to_dict()
            net_prices = mosart_wm_pmp[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.net_prices')].to_dict()

            logging.info(f"Loaded PMP calibration files for month {month} year {year}.")

            # Number of crop and NLDAS ID combinations.
            ids = range(len(mosart_wm_pmp)) 
            # Number of farm agents / NLDAS IDs.
            farm_ids = range(len(pd.unique(mosart_wm_pmp[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nldas_ids')])))

            land_constraints_by_farm = land_water_constraints_by_farm[self.config.get('water_management.demand.farmer_abm.land_water_constraints.variables.land_constraints_by_farm')].to_dict()

            crop_ids_by_farm = mosart_wm_pmp.drop(columns='index').reset_index().groupby(by=self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nldas_ids'))['index'].apply(list)
            crop_ids_by_farm.set_axis(range(0, len(crop_ids_by_farm)), inplace = True)
            crop_ids_by_farm = crop_ids_by_farm.to_dict()

            # Initialize start values to zero.
            x_start_values=dict(enumerate([0.0]*3))

            logging.info(f"Loaded constructed model indices and constraints for month {month} year {year}.")

            # 2st stage: Quadratic model included in JWP model simulations.
            # Construct model inputs.
            fwm_s = ConcreteModel()
            fwm_s.ids = Set(initialize=ids)
            fwm_s.farm_ids = Set(initialize=farm_ids)
            fwm_s.crop_ids_by_farm = Set(fwm_s.farm_ids, initialize=crop_ids_by_farm)
            fwm_s.net_prices = Param(fwm_s.ids, initialize=net_prices, mutable=True)
            fwm_s.gammas = Param(fwm_s.ids, initialize=gammas, mutable=True)
            fwm_s.land_constraints = Param(fwm_s.farm_ids, initialize=land_constraints_by_farm, mutable=True)
            fwm_s.water_constraints = Param(fwm_s.farm_ids, initialize=water_constraints_by_farm, mutable=True) #JY here need to read calculate new water constraints
            fwm_s.xs = Var(fwm_s.ids, domain=NonNegativeReals, initialize=x_start_values)
            fwm_s.nirs = Param(fwm_s.ids, initialize=nirs, mutable=True)


            # 2nd stage model: Construct functions.
            def obj_fun(fwm_s):
                # .00001 is a scaling factor for computational purposes (doesn't influence optimization results). 
                # 0.5 is part of the positive mathematical formulation equation. Both values will not vary between runs.
                return 0.00001*sum(sum((fwm_s.net_prices[i] * fwm_s.xs[i] - 0.5 * fwm_s.gammas[i] * fwm_s.xs[i] * fwm_s.xs[i]) for i in fwm_s.crop_ids_by_farm[f]) for f in fwm_s.farm_ids)
            fwm_s.obj_f = Objective(rule=obj_fun, sense=maximize)


            def land_constraint(fwm_s, ff):
                return sum(fwm_s.xs[i] for i in fwm_s.crop_ids_by_farm[ff]) <= fwm_s.land_constraints[ff]
            fwm_s.c1 = Constraint(fwm_s.farm_ids, rule=land_constraint)


            def water_constraint(fwm_s, ff):
                return sum(fwm_s.xs[i]*fwm_s.nirs[i] for i in fwm_s.crop_ids_by_farm[ff]) <= fwm_s.water_constraints[ff]
            fwm_s.c2 = Constraint(fwm_s.farm_ids, rule=water_constraint)


            logging.info(f"Constructed pyomo model for month {month} year {year}.")

            # Create and run the solver.
            try:
                opt = SolverFactory("ipopt", solver_io='nl')
                results = opt.solve(fwm_s, keepfiles=False, tee=True)
                print(results.solver.termination_condition)
                logging.info(f"Solved pyomo model for month {month} year {year}.")
            except:
                logging.info(f"Pyomo model solve has failed for month {month} year {year}.")
                return

            # Store main model outputs.
            result_xs = dict(fwm_s.xs.get_values())

            # Store results in a pandas dataframe.
            results_pd = mosart_wm_pmp.assign(**{self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.calculated_area'):result_xs.values()})
            results_pd = results_pd.assign(**{self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nir'):nirs.values()})
            results_pd[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.calculated_water_demand')] = results_pd[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.calculated_area')] * results_pd[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nir')] / ACREFTYEAR_TO_CUBICMSEC
            results_pivot = pd.pivot_table(results_pd, index=[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nldas_ids')], values=[self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.calculated_water_demand')], aggfunc=np.sum)

            # Export results to parquet.
            results_pd = results_pd[[
                self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.nldas_ids'), 
                self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.crop'), 
                self.config.get('water_management.demand.farmer_abm.mosart_wm_pmp.variables.calculated_area')
            ]]
            # Create output directory if it doesn't already exist
            try: 
                mkdir(output_dir) 
            except OSError as error: 
                print(error) 
            results_pd.to_parquet(f"{output_dir}/farmer_abm_{str(year)}.parquet")

            # Construct a DataFrame with all NLDAS IDs.
            demand_per_nldas_id = pd.DataFrame(self.model.grid.nldas_id).rename(columns={0:self.nldas_id})
            demand_per_nldas_id[self.demand] = 0

            # Use NLDAS_ID as the index and merge ABM demand.
            demand_per_nldas_id = demand_per_nldas_id.set_index(self.nldas_id,drop=False)
            demand_per_nldas_id.loc[results_pivot.index,self.demand] = results_pivot.calc_water_demand.values

            # Convert pandas DataFrame to xarray Dataset to more easily output to a NetCDF.
            demand_ABM = demand_per_nldas_id.totalDemand.values.reshape(
                len(self.model.grid.unique_latitudes), len(self.model.grid.unique_longitudes),
                order='C'
            )
            demand_ABM = xr.Dataset(
                data_vars={
                    self.demand:([self.time, self.latitude, self.longitude], np.array([demand_ABM]))
                },
                coords={
                    self.longitude: ([self.longitude], self.model.grid.unique_longitudes),
                    self.latitude: ([self.latitude], self.model.grid.unique_latitudes),
                    self.time: ([self.time], [np.datetime64(f"{year}-01")]),
                }
            )
            logging.info(f"Outputting demand file to: {output_dir}.")
            demand_ABM.to_netcdf(f"{output_dir}/{self.config.get('simulation.name')}_farmer_abm_demand_{year}.nc")
            logging.info(f"Wrote new demand files for month {month} year {year}.")

            self.processed_years.append(year)
        except Exception as e:
            logging.exception(str(e))
        
        logging.info(f"Ran farmer ABM in {pretty_timer(timer() - t)}.")