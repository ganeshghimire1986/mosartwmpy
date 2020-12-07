def update_main_channel_state(state, grid, parameters, config, base_condition):
    # update the physical properties of the main channel
    condition = grid.channel_length.gt(0) & state.channel_storage.gt(0)
    state.channel_cross_section_area = state.channel_cross_section_area.mask(
        base_condition,
        state.zeros.mask(
            condition,
            state.channel_storage / grid.channel_length
        )
    )
    # Function for estimating maximum water depth assuming rectangular channel and tropezoidal flood plain
    # here assuming the channel cross-section consists of three parts, from bottom to up,
    # part 1 is a rectangular with bankfull depth (rdep) and bankfull width (rwid)
    # part 2 is a tropezoidal, bottom width rwid and top width rwid0, height 0.1*((rwid0-rwid)/2), assuming slope is 0.1
    # part 3 is a rectagular with the width rwid0
    not_flooded = (state.channel_cross_section_area - (grid.channel_depth * grid.channel_width)).le(parameters.tiny_value)
    delta_area = state.channel_cross_section_area - grid.channel_depth  * grid.channel_width - (grid.channel_width + grid.channel_floodplain_width) * parameters.slope_1_def * ((grid.channel_floodplain_width - grid.channel_width) / 2.0) / 2.0
    equivalent_depth_condition =  delta_area.gt(parameters.tiny_value)
    state.channel_depth = state.channel_depth.mask(
        base_condition,
        state.zeros.mask(
            condition & state.channel_cross_section_area.gt(parameters.tiny_value),
            (state.channel_cross_section_area / grid.channel_width).where(
                not_flooded,
                (grid.channel_depth + parameters.slope_1_def * ((grid.channel_floodplain_width  - grid.channel_width) / 2.0) + delta_area / grid.channel_floodplain_width).where(
                    equivalent_depth_condition,
                    grid.channel_depth + (-grid.channel_width + (((grid.channel_width ** 2) + 4 * (state.channel_cross_section_area  - grid.channel_depth * grid.channel_width) / parameters.slope_1_def) ** (1/2))) * parameters.slope_1_def / 2.0
                )
            )
        )
    )
    # Function for estimating wetness perimeter based on same assumptions as above
    not_flooded = state.channel_depth.le(grid.channel_depth + parameters.tiny_value)
    delta_depth = state.channel_depth - grid.channel_depth - ((grid.channel_floodplain_width -  grid.channel_width) / 2.0) * parameters.slope_1_def
    equivalent_depth_condition = delta_depth.gt(parameters.tiny_value)
    state.channel_wetness_perimeter = state.channel_wetness_perimeter.mask(
        base_condition,
        state.zeros.mask(
            condition & state.channel_depth.ge(parameters.tiny_value),
            (grid.channel_width + 2 * state.channel_depth).where(
                not_flooded,
                (grid.channel_width + 2 * (grid.channel_depth + ((grid.channel_floodplain_width - grid.channel_width) / 2.0) * parameters.slope_1_def * parameters.inverse_sin_atan_slope_1_def + delta_depth)).where(
                    equivalent_depth_condition,
                    grid.channel_width + 2 * (grid.channel_depth + (state.channel_depth - grid.channel_depth) * parameters.inverse_sin_atan_slope_1_def)
                )
            )
        )
    )
    state.channel_hydraulic_radii = state.channel_hydraulic_radii.mask(
        base_condition,
        state.zeros.mask(
            condition & state.channel_wetness_perimeter.gt(parameters.tiny_value),
            state.channel_cross_section_area / state.channel_wetness_perimeter
        )
    )
    
    return state