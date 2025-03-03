import pandas as pd
import numpy as np
from . import util, config
from datetime import date
import sys
import re
import requests
from pathlib import Path
import json

def get_stats(time_values, start_time=None, end_time=None):
    return WaitTimeStats(time_values, start_time, end_time)

# WaitTimeStats allows computing statistics about wait times within an interval,
# (such as averages, percentiles, and histograms),
# given a sorted array of arrival or departure times (Unix timestamps in seconds),
# and optional start and end timestamps.
#
# It assumes that a person has an equal probability of showing up at the stop at
# any time within the interval.
#
# These stats are based on the area under the graph of wait time (y-axis)
# vs time person arrives at stop (x-axis). This graph consists of several line segments of
# slope -1, with zeroes at the arrival time of each bus (arrival times shown with *s below):
#
# wait time
#  ^
#  |
#  |    #                                 .                    .    #
#  |    #                                 |\         .         |\   #
#  |    #                                 | \        |\        | \  #
#  |\   #          .                      |  \       | \       |  \ #
#  | \  #   .      |\              .      |   \      |  \      |   \#
#  |  \ #   |\     | \             |\     |    \     |   \     |    \
#  |   \#   | \    |  \            | \    |     \    |    \    |    #\
#  |    \   |  \   |   \       .   |  \   |      \   |     \   |    # \
#  |    #\  |   \  |    \   .  |\  |   \  |       \  |      \  |    #  \
#  |    # \ |    \ |     \  |\ | \ |    \ |        \ |       \ |    #   \
#  |    #  \|     \|      \.| \|  \|     \|         \|        \|    #    \
#  -----#---*------*-------**--*---*------*----------*---------*----------*----> time person arrives at stop
#    interval                                                    interval
#     start                                                        end
#
# The easiest way to compute wait time statistics is by sampling the wait time
# every certain number of seconds, then calling numpy functions like np.average
# on the array of wait times. The smaller the time between samples, the more
# accurate the approximations will be, but the slower it will be to calculate.
#
# However, some statistics such as average, percentiles, and histograms
# can be computed exactly without needing to generate samples.
#
# The average wait time within the interval is the area of all triangles
# generated by buses that arrive within the interval, plus the part of the
# area within the interval of the triangle generated by the next bus arriving
# after the interval, divided by the size of the interval.
#
# The cumulative distribution function (CDF) of wait times within the interval
# can also be calculated exactly in order to compute percentiles and histograms.
#
# Example: Suppose there are 9 buses arriving within an interval of 90 minutes;
# the smallest interval between buses is 1 minute; the next smallest interval
# is 3 minutes; and the last bus arrives at the end of the interval (for simplicity).
#
# To have a wait time between 0 and 1 minute, there are 9 different
# minutes within the interval when the person could arrive at the stop, so the
# CDF for a wait time of 1 minute would be equal to 9/90 = 0.1.
#
# To have a wait time between 1 and 3 minutes, there are 8*(3-1)=16 more minutes
# when the person could arrive at a stop. Then there are a total of 16+9 = 25 minutes
# when the person could arrive with a wait time of less than 3 minutes, so the
# CDF for a wait time of 3 minutes would be equal to 25/90 = 0.2777.
#
# This process can be repeated for each headway from smallest to largest, at which time
# all times within the interval will be accounted for and the CDF will equal 1.
#
#    CDF
#      |
#  1.0 |-                                       .
#      |                            .
#      |                   .
#      |           .
#  0.5 |-     .
#      |   .
#      | .
#      |.
#  0.0 |------|---------------------------------|--> wait time
#     min  median                              max
#
# The CDF is piecewise linear between the wait times where the number of
# triangles changes.
#
# Each quantile can be calculated by finding the wait time where
# the CDF has that value (e.g. the median wait time is where the CDF=0.5)
#
# Histograms can be calculated by subtracting the value of the CDF
# at the start of each bin from the value of the CDF at the end of each bin.
# This value is equal to the fraction of times within the interval with wait
# times in that bin.
#
# Note: all returned statistics are in minutes (not seconds)
#
class WaitTimeStats:
    def __init__(self, time_values, start_time = None, end_time = None):
        self.time_values = time_values

        if len(time_values) == 0:
            self.is_empty = True
            return

        self.cdf_points = None

        first_bus_time = np.min(time_values)
        last_bus_time = np.max(time_values)

        # if start_time/end_time is outside the range of arrival times, limit interval to first/last arrival time
        interval_start = self.interval_start = int(
            max(first_bus_time, start_time) if start_time is not None else first_bus_time
        )

        interval_end = self.interval_end = int(max(
            min(last_bus_time, end_time) if end_time is not None else last_bus_time,
            interval_start
        ))

        start_arrival_index = self.start_arrival_index = np.searchsorted(time_values, self.interval_start, side='left')
        end_arrival_index = self.end_arrival_index = np.searchsorted(time_values, self.interval_end, side='left')

        if end_arrival_index > start_arrival_index:
            self.interval_time_values = interval_time_values = time_values[start_arrival_index:end_arrival_index]
            self.interval_headways = np.diff(interval_time_values, prepend=interval_start)

            # end elapsed_time is the number of seconds between when the last bus arrives within this interval and the end of the interval
            self.end_elapsed_time = interval_end - self.interval_time_values[-1]
        else:
            self.interval_time_values = np.array([])
            self.interval_headways = np.array([])
            self.end_elapsed_time = interval_end - interval_start

        if end_arrival_index < len(time_values):
            # end_wait_time is the number of seconds after the end of this interval before the next bus arrives
            self.end_wait_time = time_values[end_arrival_index] - interval_end
        else:
            self.end_wait_time = None
            if self.end_elapsed_time > 0:
                self.end_elapsed_time = 0
                self.interval_end = self.interval_time_values[-1]

        self.is_empty = (self.interval_end - self.interval_start) <= 0

    def get_average(self):
        if self.is_empty:
            return None

        total_wait = 0

        if len(self.interval_headways) > 0:
            # for the time between each bus that arrives within the interval, total wait time is area of a triangle
            total_wait += np.sum(np.square(self.interval_headways))/2

        if self.end_wait_time is not None:
            # for the next arrival after the end of the interval,
            # total wait time is the area of a rectangle plus the area of a triangle
            total_wait += (self.end_wait_time * self.end_elapsed_time) + (self.end_elapsed_time ** 2)/2

        interval_elapsed_time = self.interval_end - self.interval_start

        return total_wait / interval_elapsed_time / 60

    def get_cumulative_distribution(self):
        if self.is_empty:
            return None

        if self.cdf_points is not None:
            return self.cdf_points

        interval_elapsed_time = self.interval_end - self.interval_start

        start_arrival_index = self.start_arrival_index
        end_arrival_index = self.end_arrival_index
        interval_start = self.interval_start
        interval_end = self.interval_end

        end_wait_time = self.end_wait_time
        end_elapsed_time = self.end_elapsed_time

        interval_headways = self.interval_headways

        has_arrival = len(interval_headways) > 0

        if end_wait_time is not None:
            wait_time_values = np.r_[
                interval_headways,
                end_wait_time,
                end_wait_time + end_elapsed_time,
                0:(1 if has_arrival else 0), # only include 0 wait time if there are any arrivals within the interval
            ]
        elif has_arrival:
            wait_time_values = np.r_[0, interval_headways]
        else:
            return None

        # sorted_wait_time_values are all of the x-coordinates
        # between which the CDF is piecewise linear
        sorted_wait_time_values = np.sort(wait_time_values)

        num_wait_time_values = len(sorted_wait_time_values)

        prev_wait_time = None

        tot_elapsed = 0 # number of seconds in interval with wait time less than the current wait time

        # each point is (wait time in seconds, percentage of interval having wait time less than that).
        # CDF of wait times is piecewise linear between the returned points.
        points = []

        for i in range(0, len(sorted_wait_time_values)):
            wait_time = sorted_wait_time_values[i]

            if prev_wait_time is None or wait_time > prev_wait_time:
                num_occurrences_with_smaller_wait_time = num_wait_time_values - i
                if end_wait_time is not None and wait_time <= end_wait_time:
                    # for wait times less than or equal to end_wait_time,
                    # adjust num_occurrences_with_smaller_headway to avoid counting end_wait_time and end_wait_time + end_elapsed_time
                    # otherwise, no adjustment needed
                    num_occurrences_with_smaller_wait_time -= 2

                if prev_wait_time is None:
                    elapsed = 0
                else:
                    # the number of seconds in the interval that someone would wait between prev_wait_time and wait_time
                    elapsed = (wait_time - prev_wait_time) * num_occurrences_with_smaller_wait_time

                tot_elapsed += elapsed

                points.append((wait_time / 60, tot_elapsed / interval_elapsed_time))

                prev_wait_time = wait_time

        # if the logic above is correct,
        # the first returned point should be (min wait time, 0.0), and
        # the last returned point should be (max wait time, 1.0)
        if points[-1][1] != 1 or points[0][1] != 0:
            # should never get here unless the code is broken
            print('Invalid cumulative distribution:', file=sys.stderr)
            print(points, file=sys.stderr)
            print('Interval headways:', file=sys.stderr)
            print(interval_headways, file=sys.stderr)
            print('Sorted wait time values:', file=sys.stderr)
            print(sorted_wait_time_values, file=sys.stderr)
            print(f'End elapsed time: {end_elapsed_time}', file=sys.stderr)
            print(f'End wait time: {end_wait_time}', file=sys.stderr)
            raise AssertionError('Invalid cumulative distribution')

        self.cdf_points = np.array(points)

        return self.cdf_points

    def get_quantiles(self, quantiles):
        cdf_points = self.get_cumulative_distribution()
        if cdf_points is None:
            return None

        cdf_domain, cdf_range = cdf_points.T

        quantile_values = []

        for quantile in quantiles:
            segment_end_index = np.searchsorted(cdf_range, quantile)
            if segment_end_index == 0:
                quantile_values.append(cdf_domain[0])
            else:
                segment_start_index = segment_end_index - 1
                # linear interpolation to find wait time where value of CDF = quantile
                quantile_value = cdf_domain[segment_start_index] + \
                    (quantile - cdf_range[segment_start_index]) / \
                    (cdf_range[segment_end_index] - cdf_range[segment_start_index]) * \
                    (cdf_domain[segment_end_index] - cdf_domain[segment_start_index])
                quantile_values.append(quantile_value)

        return np.array(quantile_values)

    def get_percentiles(self, percentiles):
        return self.get_quantiles(np.array(percentiles) / 100)

    def get_histogram(self, bins):
        cdf_points = self.get_cumulative_distribution()
        if cdf_points is None:
            return None

        cdf_domain, cdf_range = cdf_points.T

        histogram = []
        prev_cumulative_value = None
        for bin_index, bin_value in enumerate(bins):
            cumulative_value = self._get_probability_less_than(bin_value, cdf_domain, cdf_range)

            if prev_cumulative_value is not None:
                histogram.append(cumulative_value - prev_cumulative_value)

            prev_cumulative_value = cumulative_value

        return np.array(histogram)

    def get_probability_less_than(self, wait_time):
        cdf_points = self.get_cumulative_distribution()
        if cdf_points is None:
            return None

        cdf_domain, cdf_range = cdf_points.T

        return self._get_probability_less_than(wait_time, cdf_domain, cdf_range)

    def get_probability_greater_than(self, wait_time):
        prob_less = self.get_probability_less_than(wait_time)

        if prob_less is None:
            return None

        return 1.0 - prob_less

    def _get_probability_less_than(self, wait_time, cdf_domain, cdf_range):
        segment_end_index = np.searchsorted(cdf_domain, wait_time)

        if segment_end_index >= len(cdf_domain):
            return 1.0
        elif segment_end_index == 0:
            return 0.0
        else:
            segment_start_index = segment_end_index - 1

            # linear interpolation to find value of CDF with wait time = bin_value
            return cdf_range[segment_start_index] + \
                (wait_time - cdf_domain[segment_start_index]) / \
                (cdf_domain[segment_end_index] - cdf_domain[segment_start_index]) * \
                (cdf_range[segment_end_index] - cdf_range[segment_start_index])

    def get_sampled_waits(self, sample_sec=60):
        if self.is_empty:
            return None

        sample_time_values = np.arange(
            self.interval_start - (self.interval_start % sample_sec), self.interval_end, sample_sec
        )

        if self.end_wait_time is not None:
            arrival_time_values = np.r_[self.interval_time_values, self.interval_end + self.end_wait_time, np.nan]
        else:
            arrival_time_values = np.r_[self.interval_time_values, np.nan]

        next_arrival_indexes = np.searchsorted(arrival_time_values, sample_time_values, 'left')

        next_arrival_times = arrival_time_values[next_arrival_indexes]

        waits = next_arrival_times - sample_time_values

        return waits[np.logical_not(np.isnan(waits))] / 60

DefaultVersion = 'v1b'

class CachedWaitTimes:
    def __init__(self, wait_times_data):
        self.wait_times_data = wait_times_data

    def get_value(self, route_id, direction_id, stop_id):
        routes_data = self.wait_times_data['routes']

        if route_id not in routes_data:
            return None

        route_data = routes_data[route_id]

        if direction_id not in route_data:
            return None

        direction_data = route_data[direction_id]

        if stop_id not in direction_data:
            return None

        return direction_data[stop_id]

def get_cached_wait_times(agency_id, d: date, stat_id: str, start_time_str = None, end_time_str = None, version = DefaultVersion) -> CachedWaitTimes:

    cache_path = get_cache_path(agency_id, d, stat_id, start_time_str, end_time_str, version)

    try:
        with open(cache_path, "r") as f:
            text = f.read()
            return CachedWaitTimes(json.loads(text))
    except FileNotFoundError as err:
        pass

    s3_bucket = config.s3_bucket
    s3_path = get_s3_path(agency_id, d, stat_id, start_time_str, end_time_str, version)

    s3_url = f"http://{s3_bucket}.s3.amazonaws.com/{s3_path}"
    r = requests.get(s3_url)

    if r.status_code == 404:
        raise FileNotFoundError(f"{s3_url} not found")
    if r.status_code == 403:
        raise FileNotFoundError(f"{s3_url} not found or access denied")
    if r.status_code != 200:
        raise Exception(f"Error fetching {s3_url}: HTTP {r.status_code}: {r.text}")

    data = json.loads(r.text)

    cache_dir = Path(cache_path).parent
    if not cache_dir.exists():
        cache_dir.mkdir(parents = True, exist_ok = True)

    with open(cache_path, "w") as f:
        f.write(r.text)

    return CachedWaitTimes(data)

def get_time_range_path(start_time_str, end_time_str):
    if start_time_str is None and end_time_str is None:
        return ''
    else:
        return f'_{start_time_str.replace(":","")}_{end_time_str.replace(":","")}'

def get_s3_path(agency_id: str, d: date, stat_id: str, start_time_str, end_time_str, version = DefaultVersion) -> str:
    time_range_path = get_time_range_path(start_time_str, end_time_str)
    date_str = str(d)
    date_path = d.strftime("%Y/%m/%d")
    return f"wait-times/{version}/{agency_id}/{date_path}/wait-times_{version}_{agency_id}_{date_str}_{stat_id}{time_range_path}.json.gz"

def get_cache_path(agency_id: str, d: date, stat_id: str, start_time_str, end_time_str, version = DefaultVersion) -> str:
    time_range_path = get_time_range_path(start_time_str, end_time_str)

    date_str = str(d)
    if re.match('^[\w\-]+$', agency_id) is None:
        raise Exception(f"Invalid agency: {agency_id}")

    if re.match('^[\w\-]+$', date_str) is None:
        raise Exception(f"Invalid date: {date_str}")

    if re.match('^[\w\-]+$', version) is None:
        raise Exception(f"Invalid version: {version}")

    if re.match('^[\w\-]+$', stat_id) is None:
        raise Exception(f"Invalid stat id: {stat_id}")

    if re.match('^[\w\-\+]*$', time_range_path) is None:
        raise Exception(f"Invalid time range: {time_range_path}")

    return f'{util.get_data_dir()}/wait-times_{version}_{agency_id}/{date_str}/wait-times_{version}_{agency_id}_{date_str}_{stat_id}{time_range_path}.json'
