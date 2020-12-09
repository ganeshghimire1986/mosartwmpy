import numpy as np
import pandas as pd

def update_subnetwork_state(state, grid, parameters, config, base_condition):
    # update the physical properties of the subnetwork
        
    # update state variables
    condition = (grid.subnetwork_length.values > 0) & (state.subnetwork_storage.values > 0)
    state.subnetwork_cross_section_area = pd.DataFrame(np.where(
        base_condition,
        np.where(
            condition,
            state.subnetwork_storage.values / grid.subnetwork_length.values,
            0
        ),
        state.subnetwork_cross_section_area.values
    ))
    state.subnetwork_depth = pd.DataFrame(np.where(
        base_condition,
        np.where(
            condition & (state.subnetwork_cross_section_area > parameters.tiny_value),
            state.subnetwork_cross_section_area.values / grid.subnetwork_width.values,
            0
        ),
        state.subnetwork_depth.values
    ))
    state.subnetwork_wetness_perimeter = pd.DataFrame(np.where(
        base_condition,
        np.where(
            condition & (state.subnetwork_depth.values > parameters.tiny_value),
            grid.subnetwork_width.values + 2 * state.subnetwork_depth.values,
            0
        ),
        state.subnetwork_wetness_perimeter.values
    ))
    state.subnetwork_hydraulic_radii = pd.DataFrame(np.where(
        base_condition,
        np.where(
            condition & (state.subnetwork_wetness_perimeter.values > parameters.tiny_value),
            state.subnetwork_cross_section_area.values / state.subnetwork_wetness_perimeter.values,
            0
        ),
        state.subnetwork_hydraulic_radii.values
    ))
    
    return state